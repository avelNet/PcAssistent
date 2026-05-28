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
        progress_tracker=None,
    ):
        self.config = config
        self.git_watcher = git_watcher
        self.clipboard_watcher = clipboard_watcher
        self.jetbrains_watcher = jetbrains_watcher
        self.stats_builder = stats_builder
        self.progress_tracker = progress_tracker
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

    async def build(
        self,
        trigger: str,
        extra: dict | None = None,
        project_override: str | None = None,
    ) -> dict:
        """
        Собрать контекст для LLM из всех источников.
        project_override — принудительный фокус (для фонового анализа других проектов).
        Возвращает словарь который передаётся в prompt_engine.build().
        """
        logger.info("ContextBuilder: собираю контекст для триггера '%s'", trigger)
        ctx: dict = {}

        # Фокус определяем ДО сбора данных — нужен для фильтрации ошибок
        focus = project_override or get_focus()

        # Запускаем все источники параллельно
        source_tasks = {
            "git":          self._get_git_context(),
            "history":      self._get_task_history(),
            "today_tasks":  context_store.get_tasks_for_date(),
            "errors":       self._get_errors_context(focus),
            "obsidian":     self._get_vault_context(),
            "clipboard":    self._get_clipboard_context(),
            "ide":          self._get_jetbrains_context(),
            "productivity": self._get_productivity_context(),
            "progress":     self._get_progress_context(),
        }

        results = await asyncio.gather(*source_tasks.values(), return_exceptions=True)

        for key, result in zip(source_tasks.keys(), results):
            if isinstance(result, Exception):
                logger.warning("ContextBuilder: ошибка источника '%s': %s", key, result)
                ctx[key] = [] if key != "obsidian" else {}
            else:
                ctx[key] = result

        if extra:
            ctx.update(extra)

        if focus:
            ctx["focus"] = focus
            if project_override:
                logger.debug("ContextBuilder: фоновый анализ → %s", focus)
            else:
                logger.info("ContextBuilder: активен фокус → %s", focus)

        ctx = self._trim_context(ctx)

        sources = [k for k, v in ctx.items() if v]
        logger.info("ContextBuilder: контекст готов, источники: %s", ", ".join(sources))
        return ctx

    async def get_known_projects(self) -> list[str]:
        """Список всех известных проектов из GitWatcher."""
        if not self.git_watcher:
            return []
        snapshots = await self.git_watcher.get_all_snapshots()
        return [s["name"] for s in snapshots if s.get("name")]

    async def get_project_git_path(self, project: str) -> str | None:
        """Абсолютный путь к репозиторию проекта (по имени папки)."""
        if not self.git_watcher:
            return None
        snapshots = await self.git_watcher.get_all_snapshots()
        for s in snapshots:
            if s.get("name") == project:
                return s.get("path")
        return None

    async def get_project_snapshot(self, project: str) -> dict | None:
        """Полный git-снапшот для проекта по имени."""
        if not self.git_watcher:
            return None
        snapshots = await self.git_watcher.get_all_snapshots()
        for s in snapshots:
            if s.get("name") == project:
                return s
        return None

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

    async def _get_errors_context(self, focus: str | None) -> list[dict]:
        """
        Ошибки только из активного проекта (по пути файла).
        Без фокуса — возвращаем пустой список: не путаем LLM чужими ошибками.
        """
        if not focus:
            return []
        all_errors = await context_store.get_errors(hours=24)
        # Фильтруем: путь файла должен содержать имя проекта
        project_errors = [
            e for e in all_errors
            if focus.lower() in (e.get("file") or "").lower()
        ]
        if len(project_errors) < len(all_errors):
            logger.debug(
                "ContextBuilder: ошибки отфильтрованы по проекту '%s': %d → %d",
                focus, len(all_errors), len(project_errors),
            )
        return project_errors

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

    async def _get_progress_context(self) -> dict:
        """Статистика выполнения задач за 7 дней (зависшие, заброшенные проекты)."""
        if not self.progress_tracker:
            return {}
        try:
            return await self.progress_tracker.build()
        except Exception as e:
            logger.debug("ContextBuilder: progress_tracker ошибка — %s", e)
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
