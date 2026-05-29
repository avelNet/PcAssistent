"""
orchestrator.py — точка сборки системы.
Инициализирует модули в правильном порядке, управляет lifecycle.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from core.event_bus import EventBus
from core.trigger_engine import TriggerEngine
from collectors.git_watcher import GitWatcher
from collectors.process_monitor import ProcessMonitor
from collectors.fs_watcher import FSWatcher
from collectors.clipboard_watcher import ClipboardWatcher
from collectors.jetbrains_watcher import JetBrainsWatcher
from errors.error_store import ErrorStore
from errors.static_analyzer import StaticAnalyzer
from productivity.focus_analyzer import FocusAnalyzer
from productivity.session_tracker import SessionTracker
from productivity.stats_builder import StatsBuilder
from obsidian.client import ObsidianClient
from obsidian.task_syncer import TaskSyncer
from obsidian.watcher import ObsidianWatcher
from obsidian.progress_tracker import ProgressTracker
from llm.context_builder import ContextBuilder
from llm.client_factory import create_llm_client
from ui.tray_app import TrayApp
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
        self.fs_watcher: FSWatcher | None = None
        self.clipboard_watcher: ClipboardWatcher | None = None
        self.jetbrains_watcher: JetBrainsWatcher | None = None
        self.error_store: ErrorStore | None = None
        self.static_analyzer: StaticAnalyzer | None = None
        self.focus_analyzer: FocusAnalyzer | None = None
        self.session_tracker: SessionTracker | None = None
        self.stats_builder: StatsBuilder | None = None
        self.llm_client = None
        self.context_builder: ContextBuilder | None = None
        self.trigger_engine: TriggerEngine | None = None
        self.obsidian: ObsidianClient | None = None
        self.task_syncer: TaskSyncer | None = None
        self.obsidian_watcher: ObsidianWatcher | None = None
        self.progress_tracker: ProgressTracker | None = None
        self.tray_app: TrayApp | None = None

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

        # 3c. FS watcher — inotify на рабочие директории
        self.fs_watcher = FSWatcher(self.config, self.bus)
        await self.fs_watcher.start()

        # 3d. Clipboard watcher
        self.clipboard_watcher = ClipboardWatcher(self.config, self.bus)
        await self.clipboard_watcher.start()

        # 3e. JetBrains watcher
        self.jetbrains_watcher = JetBrainsWatcher(self.config, self.bus)
        await self.jetbrains_watcher.start()

        # 3f. Productivity — focus + session
        self.focus_analyzer  = FocusAnalyzer(self.config, self.bus)
        self.session_tracker = SessionTracker(self.config, self.bus)
        self.session_tracker.subscribe()
        await self.focus_analyzer.start()

        # 3g. Errors — error_store + static_analyzer
        self.error_store = ErrorStore(self.config, self.bus)
        self.error_store.subscribe()
        self.static_analyzer = StaticAnalyzer(self.config, self.bus)
        self.static_analyzer.subscribe()

        # 4. LLM клиенты — два: лёгкий (частые запросы) и тяжёлый (сложные задачи)
        # auto   = openrouter → groq  (focus_switch, user_returned, errors_spike)
        # heavy  = groq → openrouter  (morning_briefing, evening_summary, manual)
        self.llm_client = create_llm_client(self.config)   # provider из config (auto)
        heavy_cfg = {**self.config, "llm": {"provider": "heavy"}}
        self.llm_heavy = create_llm_client(heavy_cfg)

        for client in (self.llm_client, self.llm_heavy):
            if hasattr(client, "start_background_probe"):
                client.start_background_probe()

        # 4b. StatsBuilder (нужен session_tracker и git_watcher)
        self.stats_builder = StatsBuilder(
            session_tracker=self.session_tracker,
            git_watcher=self.git_watcher,
        )

        # 4c. ProgressTracker — статистика задач за 7 дней (нужен до context_builder)
        self.progress_tracker = ProgressTracker(days=7)

        # 5. Сборщик контекста
        self.context_builder = ContextBuilder(
            self.config,
            git_watcher=self.git_watcher,
            clipboard_watcher=self.clipboard_watcher,
            jetbrains_watcher=self.jetbrains_watcher,
            stats_builder=self.stats_builder,
            progress_tracker=self.progress_tracker,
        )

        # 6. TriggerEngine — подписывается на события
        self.trigger_engine = TriggerEngine(
            self.config, self.bus, self.llm_client, self.context_builder,
            llm_heavy=self.llm_heavy,
        )

        # 7. Obsidian клиент + синхронизатор задач
        self.obsidian = ObsidianClient(self.config)
        self.task_syncer = TaskSyncer(self.config, self.bus, self.obsidian)

        # 7b. Obsidian watcher (inotify на vault)
        self.obsidian_watcher = ObsidianWatcher(self.config, self.bus)
        await self.obsidian_watcher.start()

        # 7d. UI трей
        self.tray_app = TrayApp(self.config, self.bus)
        self.tray_app.subscribe()
        await self.tray_app.start()

        # 7f. При старте — синхронизировать текущие задачи из Obsidian в SQLite
        if self.obsidian.enabled:
            synced = await self.task_syncer.sync_from_obsidian()
            if synced:
                logger.info("Orchestrator: синхронизировано %d задач из Obsidian", synced)

            # 7g. Rollover — перенести незакрытые задачи активного проекта
            # если сегодняшний файл ещё не создан (смена даты)
            from storage.focus_store import get_focus
            focus = get_focus()
            if focus:
                await self.bus.emit("trigger.rollover", {"project": focus})

            # 7i. Прогрев TTS в фоне — модель ~358 MB, грузится 5 сек,
            # первый speak() без прогрева теряет начало фразы
            from voice.speech_output import SpeechOutput
            sp = SpeechOutput(self.config)
            asyncio.create_task(sp.tts.warmup(), name="tts_warmup")


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

        # Останавливаем watchdog и фоновые модули
        if self.git_watcher:
            await asyncio.to_thread(self.git_watcher.stop)
        if self.fs_watcher:
            await asyncio.to_thread(self.fs_watcher.stop)
        if self.obsidian_watcher:
            await asyncio.to_thread(self.obsidian_watcher.stop)
        if self.clipboard_watcher:
            self.clipboard_watcher.stop()
        if self.jetbrains_watcher:
            self.jetbrains_watcher.stop()
        if self.focus_analyzer:
            self.focus_analyzer.stop()
        if self.tray_app:
            self.tray_app.stop()

        # Закрываем Obsidian клиент
        if self.obsidian:
            await self.obsidian.close()

        # Закрываем LLM клиенты
        for client in (self.llm_client, getattr(self, "llm_heavy", None)):
            if client:
                if hasattr(client, "unload_model"):
                    await client.unload_model()
                await client.close()

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
                    target += timedelta(days=1)
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
