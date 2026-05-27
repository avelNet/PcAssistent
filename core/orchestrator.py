"""
orchestrator.py — точка сборки системы.
Инициализирует модули в правильном порядке, управляет lifecycle.
"""

import asyncio
import logging
import signal
from datetime import datetime, time as dt_time
from pathlib import Path

import yaml

from core.event_bus import EventBus
from core.trigger_engine import TriggerEngine
from collectors.git_watcher import GitWatcher
from collectors.process_monitor import ProcessMonitor
from llm.context_builder import ContextBuilder
from llm.client_factory import create_llm_client
from obsidian.client import ObsidianClient
from obsidian.task_syncer import TaskSyncer
from obsidian.watcher import ObsidianWatcher
from storage import db, context_store

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yaml") -> dict:
    """
    Загрузить config.yaml и опционально config.local.yaml.
    config.local.yaml — для секретов (api_key и т.п.), он в .gitignore.
    Значения из local переопределяют базовые (глубокий merge).
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Конфиг не найден: {path.resolve()}")
    with open(path) as f:
        config = yaml.safe_load(f) or {}

    # Мержим config.local.yaml если есть
    local_path = path.parent / "config.local.yaml"
    if local_path.exists():
        with open(local_path) as f:
            local = yaml.safe_load(f) or {}
        _deep_merge(config, local)
        logger.info("Конфиг загружен: %s + %s", path.name, local_path.name)
    else:
        logger.info("Конфиг загружен: %s", path.resolve())

    return config


def _deep_merge(base: dict, override: dict) -> None:
    """Рекурсивно мержит override в base (изменяет base на месте)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


class Orchestrator:
    def __init__(self, config: dict):
        self.config = config
        self.bus = EventBus()
        self._tasks: list[asyncio.Task] = []

        # Модули (инициализируются в start())
        self.git_watcher: GitWatcher | None = None
        self.process_monitor: ProcessMonitor | None = None
        self.llm_client = None
        self.context_builder: ContextBuilder | None = None
        self.trigger_engine: TriggerEngine | None = None
        self.obsidian: ObsidianClient | None = None
        self.task_syncer: TaskSyncer | None = None
        self.obsidian_watcher: ObsidianWatcher | None = None

    async def start(self) -> None:
        """Инициализировать и запустить все модули."""
        logger.info("Orchestrator: старт...")

        # 1. БД
        db_path = self.config.get("storage", {}).get("db_path", "~/.local/share/pc-assistant/db.sqlite")
        await db.init(db_path)

        # 2. Очистка устаревших данных
        ttl = self.config.get("storage", {}).get("ttl_days", 7)
        await context_store.cleanup(ttl)

        # 3. Git-коллектор
        self.git_watcher = GitWatcher(self.config, self.bus)
        await self.git_watcher.start()

        # 3b. Process monitor — screen lock, сессии, idle
        self.process_monitor = ProcessMonitor(self.config, self.bus)
        await self.process_monitor.start()

        # 4. LLM клиент (Ollama или OpenRouter — зависит от config.llm.provider)
        self.llm_client = create_llm_client(self.config)

        # Для OpenRouter: фоновый пробинг чтобы к первому реальному запросу
        # уже знать рабочую модель (не тратить время на ротацию)
        if hasattr(self.llm_client, "start_background_probe"):
            self.llm_client.start_background_probe()

        # 5. Сборщик контекста
        self.context_builder = ContextBuilder(self.config, git_watcher=self.git_watcher)

        # 6. TriggerEngine — подписывается на события
        self.trigger_engine = TriggerEngine(
            self.config, self.bus, self.llm_client, self.context_builder
        )

        # 7. Obsidian клиент + синхронизатор задач
        self.obsidian = ObsidianClient(self.config)
        self.task_syncer = TaskSyncer(self.config, self.bus, self.obsidian)

        # 7b. Obsidian watcher (inotify на vault)
        self.obsidian_watcher = ObsidianWatcher(self.config, self.bus)
        await self.obsidian_watcher.start()

        # 7c. При старте — синхронизировать текущие задачи из Obsidian в SQLite
        if self.obsidian.enabled:
            synced = await self.task_syncer.sync_from_obsidian()
            if synced:
                logger.info("Orchestrator: синхронизировано %d задач из Obsidian", synced)

        # 8. Подписка на результат LLM (для логирования в Day 1)
        self.bus.on("llm.completed", self._on_llm_completed)

        # 8. Планирование ночной очистки
        self._tasks.append(
            asyncio.create_task(self._midnight_cleanup_loop(), name="midnight_cleanup")
        )

        logger.info("Orchestrator: все модули запущены ✓")

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Orchestrator: остановка...")

        # Останавливаем задачи
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Останавливаем watchdog
        if self.git_watcher:
            await asyncio.to_thread(self.git_watcher.stop)
        if self.obsidian_watcher:
            await asyncio.to_thread(self.obsidian_watcher.stop)

        # Закрываем Obsidian клиент
        if self.obsidian:
            await self.obsidian.close()

        # Закрываем LLM клиент (Ollama — выгружает модель из RAM, OpenRouter — закрывает сессию)
        if self.llm_client:
            if hasattr(self.llm_client, "unload_model"):
                await self.llm_client.unload_model()
            await self.llm_client.close()

        # Закрываем БД
        await db.close()

        logger.info("Orchestrator: остановлен")

    async def _on_llm_completed(self, data: dict) -> None:
        """Обработчик завершения LLM — в Day 1 просто красиво логируем."""
        if not data:
            return
        prologue = data.get("prologue", "")
        tasks = data.get("tasks", [])
        trigger = data.get("trigger", "?")

        print("\n" + "═" * 60)
        print(f"🤖 Ассистент [{trigger}]")
        if prologue:
            print(f"\n{prologue}\n")
        if tasks:
            print("📋 Задачи:")
            priority_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}
            for i, t in enumerate(tasks, 1):
                emoji = priority_emoji.get(t["priority"], "⚪")
                project = f" [{t['project']}]" if t.get("project") else ""
                print(f"  {i}. {emoji} {t['title']}{project}")
                if t.get("description"):
                    print(f"     └─ {t['description']}")
        else:
            print("  (задачи не сгенерированы)")
        print("═" * 60 + "\n")

    async def _midnight_cleanup_loop(self) -> None:
        """Запускать cleanup() каждую ночь в 03:00."""
        while True:
            try:
                now = datetime.now()
                target = now.replace(hour=3, minute=0, second=0, microsecond=0)
                if now >= target:
                    # Уже прошли 03:00 сегодня — ждём завтра
                    target = target.replace(day=target.day + 1)
                wait_sec = (target - now).total_seconds()
                logger.debug("Следующая очистка БД через %.0f сек (в 03:00)", wait_sec)
                await asyncio.sleep(wait_sec)

                ttl = self.config.get("storage", {}).get("ttl_days", 7)
                await context_store.cleanup(ttl)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Ошибка в midnight_cleanup_loop")
                await asyncio.sleep(3600)  # если что-то пошло не так — повторить через час
