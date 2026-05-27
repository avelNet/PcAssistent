"""
context_builder.py — собирает контекст для LLM из всех источников.
"""

import asyncio
import logging
from datetime import date

from storage import context_store
from storage.focus_store import get_focus

logger = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(
        self,
        config: dict,
        git_watcher=None,
        clipboard_watcher=None,
        jetbrains_watcher=None,
        stats_builder=None,
    ):
        self.config = config
        self.git_watcher = git_watcher
        self.clipboard_watcher = clipboard_watcher
        self.jetbrains_watcher = jetbrains_watcher
        self.stats_builder = stats_builder
        self.ollama_num_ctx = config.get("ollama", {}).get("num_ctx", 8192)

        # Vault reader — инициализируем если папка существует
        self._vault_reader = self._init_vault_reader()

    def _init_vault_reader(self):
        obs_cfg = self.config.get("obsidian", {})
        root = obs_cfg.get("root")
        if not root:
            return None
        try:
            from obsidian.vault_reader import VaultReader
            reader = VaultReader(self.config)
            if reader.root.exists():
                logger.info("ContextBuilder: vault reader готов (%s)", reader.root)
                return reader
            else:
                logger.debug("ContextBuilder: vault root не найден: %s", reader.root)
        except Exception as e:
            logger.warning("ContextBuilder: не удалось инициализировать vault reader: %s", e)
        return None

    async def build(self, trigger: str, extra: dict | None = None) -> dict:
        """
        Собрать контекст для LLM из всех источников.
        Возвращает словарь который передаётся в prompt_engine.build().
        """
        logger.info("ContextBuilder: собираю контекст для триггера '%s'", trigger)
        ctx: dict = {}

        # Запускаем все источники параллельно
        tasks = {
            "git":          self._get_git_context(),
            "history":      self._get_task_history(),
            "today_tasks":  context_store.get_tasks_for_date(),
            "errors":       context_store.get_errors(hours=24),
            "obsidian":     self._get_vault_context(),
            "clipboard":    self._get_clipboard_context(),
            "ide":          self._get_jetbrains_context(),
            "productivity": self._get_productivity_context(),
        }

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("ContextBuilder: ошибка источника '%s': %s", key, result)
                ctx[key] = [] if key != "obsidian" else {}
            else:
                ctx[key] = result

        if extra:
            ctx.update(extra)

        # Добавляем текущий фокус — prompt_engine использует его для фильтрации
        focus = get_focus()
        if focus:
            ctx["focus"] = focus
            logger.info("ContextBuilder: активен фокус → %s", focus)

        ctx = self._trim_context(ctx)

        sources = [k for k, v in ctx.items() if v]
        logger.info("ContextBuilder: контекст готов, источники: %s", ", ".join(sources))
        return ctx

    # ─── Источники ──────────────────────────────────────────────────────────

    async def _get_git_context(self) -> list[dict]:
        """Актуальные снапшоты репозиториев."""
        if self.git_watcher:
            snapshots = await self.git_watcher.get_all_snapshots()
            if snapshots:
                return snapshots

        # Фолбэк: последние из SQLite
        recent = await context_store.get_all_recent(hours=48)
        return [
            item["data"] for item in recent
            if item["source"] == "git" and isinstance(item["data"], dict)
        ]

    async def _get_task_history(self) -> list[dict]:
        return await context_store.get_task_history(days=3)

    async def _get_clipboard_context(self) -> list[dict]:
        """Последние записи буфера обмена."""
        if not self.clipboard_watcher:
            return []
        return self.clipboard_watcher.get_history(limit=10)

    async def _get_jetbrains_context(self) -> dict:
        """Снапшот JetBrains IDE."""
        if not self.jetbrains_watcher:
            return {}
        try:
            return await self.jetbrains_watcher.get_snapshot()
        except Exception as e:
            logger.debug("ContextBuilder: jetbrains snapshot ошибка — %s", e)
            return {}

    async def _get_productivity_context(self) -> dict:
        """Метрики продуктивности текущей сессии."""
        if not self.stats_builder:
            return {}
        try:
            return await self.stats_builder.build()
        except Exception as e:
            logger.debug("ContextBuilder: stats_builder ошибка — %s", e)
            return {}

    async def _get_vault_context(self) -> dict:
        """
        Читает все Obsidian заметки из root-папки.
        Передаёт git-снапшоты для умного отбора связанных заметок.
        Возвращает контекст по проектам.
        """
        if not self._vault_reader:
            return {}

        try:
            # Получаем git-данные для сигнала релевантности заметок
            git_snapshots = await self._get_git_context()

            context = await asyncio.to_thread(
                self._vault_reader.build_context, git_snapshots
            )
            total = context.get("total_notes", 0)
            selected = context.get("selected_notes", 0)
            projects = list(context.get("projects", {}).keys())
            logger.info(
                "ContextBuilder: vault — отобрано %d/%d заметок из %d проектов: %s",
                selected, total, len(projects), ", ".join(projects)
            )
            return context
        except Exception:
            logger.exception("ContextBuilder: ошибка чтения vault")
            return {}

    # ─── Обрезка контекста ───────────────────────────────────────────────────

    def _trim_context(self, ctx: dict) -> dict:
        """
        Если контекст не влезает в num_ctx — обрезаем по приоритету.
        Порядок обрезки (от менее важного к более важному):
          1. Содержимое заметок Obsidian → только структура
          2. История задач → только сегодня
          3. Git → убираем todos и recent_log
          4. Ошибки → только топ-5
        """
        import json

        # 70% окна — под контекст, остальное — системный промпт + ответ
        max_chars = self.ollama_num_ctx * 4 * 0.7

        def size():
            return len(json.dumps(ctx, ensure_ascii=False, default=str))

        if size() <= max_chars:
            return ctx

        logger.warning("ContextBuilder: контекст слишком большой (%d символов), обрезаем", size())

        # 1. Obsidian: сначала режем до 300 символов на заметку
        if ctx.get("obsidian", {}).get("projects"):
            for project_notes in ctx["obsidian"]["projects"].values():
                for note in project_notes:
                    if len(note.get("content", "")) > 300:
                        note["content"] = note["content"][:300] + "..."
            if size() <= max_chars:
                return ctx

        # 1b. Всё ещё большой — оставляем только недавние заметки (≤3 дней)
        if ctx.get("obsidian", {}).get("projects"):
            for proj in ctx["obsidian"]["projects"]:
                ctx["obsidian"]["projects"][proj] = [
                    n for n in ctx["obsidian"]["projects"][proj]
                    if n.get("modified_days_ago", 999) <= 3
                ]
            if size() <= max_chars:
                return ctx

        # 1c. Всё ещё большой — убираем содержимое, только пути
        if ctx.get("obsidian", {}).get("projects"):
            for project_notes in ctx["obsidian"]["projects"].values():
                for note in project_notes:
                    note["content"] = ""
            if size() <= max_chars:
                return ctx

        # 2. История задач → только сегодня
        if ctx.get("history"):
            today = date.today().isoformat()
            ctx["history"] = [t for t in ctx["history"] if t.get("date") == today]
            if size() <= max_chars:
                return ctx

        # 3. Git → убираем todos и recent_log
        for repo in ctx.get("git", []):
            repo.pop("todos", None)
            repo["recent_log"] = ""
        if size() <= max_chars:
            return ctx

        # 4. Ошибки → топ-5
        if ctx.get("errors"):
            ctx["errors"] = ctx["errors"][:5]

        return ctx
