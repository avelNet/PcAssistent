"""
context_builder.py — собирает контекст для LLM из всех источников.

Для Day 1: только git данные.
Day 2+: добавятся IDE, ошибки, продуктивность, Obsidian.
"""

import logging
from datetime import date

from storage import context_store

logger = logging.getLogger(__name__)


class ContextBuilder:
    def __init__(self, config: dict, git_watcher=None):
        self.config = config
        self.git_watcher = git_watcher
        self.ollama_num_ctx = config.get("ollama", {}).get("num_ctx", 8192)

    async def build(self, trigger: str, extra: dict | None = None) -> dict:
        """
        Собрать контекст для LLM.
        Возвращает словарь который передаётся в prompt_engine.build().
        """
        logger.info("ContextBuilder: собираю контекст для триггера '%s'", trigger)
        ctx: dict = {}

        # Git снапшоты
        ctx["git"] = await self._get_git_context()

        # История задач (3 дня)
        ctx["history"] = await self._get_task_history()

        # Задачи на сегодня
        ctx["today_tasks"] = await context_store.get_tasks_for_date()

        # Ошибки
        ctx["errors"] = await context_store.get_errors(hours=24)

        # Дополнительный контекст от триггера (например, спайк ошибок)
        if extra:
            ctx.update(extra)

        # Приоритизация: если контекст большой — обрезаем менее важное
        ctx = self._trim_context(ctx)

        logger.info("ContextBuilder: контекст готов (%d источников)", len(ctx))
        return ctx

    async def _get_git_context(self) -> list[dict]:
        """Получить актуальные снапшоты репозиториев."""
        if self.git_watcher:
            # Актуальные данные прямо сейчас
            snapshots = await self.git_watcher.get_all_snapshots()
            if snapshots:
                return snapshots

        # Фолбэк: последние сохранённые в SQLite
        recent = await context_store.get_all_recent(hours=48)
        git_snapshots = []
        for item in recent:
            if item["source"] == "git":
                data = item["data"]
                if isinstance(data, dict):
                    git_snapshots.append(data)
        return git_snapshots

    async def _get_task_history(self) -> list[dict]:
        """История задач за 3 дня."""
        return await context_store.get_task_history(days=3)

    def _trim_context(self, ctx: dict) -> dict:
        """
        Обрезать контекст если он слишком большой.
        Порядок обрезки: clipboard → history → fs events.
        Оцениваем по количеству символов (грубо ~4 chars/token).
        """
        import json
        max_chars = self.ollama_num_ctx * 4 * 0.7  # 70% окна под контекст

        total = len(json.dumps(ctx, ensure_ascii=False, default=str))

        if total <= max_chars:
            return ctx

        logger.warning(
            "ContextBuilder: контекст %d символов > лимит %d, обрезаем",
            total, max_chars
        )

        # 1. Обрезаем clipboard
        ctx.pop("clipboard", None)
        total = len(json.dumps(ctx, ensure_ascii=False, default=str))
        if total <= max_chars:
            return ctx

        # 2. Обрезаем историю задач до 1 дня
        if ctx.get("history"):
            today = date.today().isoformat()
            ctx["history"] = [t for t in ctx["history"] if t.get("date") == today]
        total = len(json.dumps(ctx, ensure_ascii=False, default=str))
        if total <= max_chars:
            return ctx

        # 3. Обрезаем git: убираем todos и recent_log
        for repo in ctx.get("git", []):
            repo.pop("todos", None)
            repo["recent_log"] = ""
        total = len(json.dumps(ctx, ensure_ascii=False, default=str))
        if total <= max_chars:
            return ctx

        # 4. Оставляем только последние 5 ошибок
        if ctx.get("errors"):
            ctx["errors"] = ctx["errors"][:5]

        return ctx
