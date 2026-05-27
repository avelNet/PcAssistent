"""
obsidian/task_syncer.py — двусторонняя синхронизация задач: SQLite ↔ Obsidian.

LLM → Obsidian:
  Слушает llm.completed → groupит задачи по проекту → пишет дейли заметки.
  Триггер evening_summary → дозаписывает итог дня.

Obsidian → SQLite:
  Слушает obsidian.daily_changed → читает чекбоксы через client → обновляет БД.
  Obsidian имеет приоритет: если там [ ] а в SQLite done=1 — сбрасываем (§3.21).
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from storage import context_store

if TYPE_CHECKING:
    from obsidian.client import ObsidianClient
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

# Триггеры где создаём новую дейли (а не дозаписываем)
_MORNING_TRIGGERS = {
    "morning_briefing",
    "manual",
    "git_activity",
    "errors_spike",
    "user_returned",
    "after_work_session",
}


class TaskSyncer:
    def __init__(self, config: dict, bus: "EventBus", client: "ObsidianClient"):
        self.config = config.get("obsidian", {})
        self.bus = bus
        self.client = client
        self._subscribe()
        logger.debug("TaskSyncer: инициализирован")

    def _subscribe(self) -> None:
        self.bus.on("llm.completed",         self._on_llm_completed)
        self.bus.on("obsidian.daily_changed", self._on_daily_changed)

    # ─── LLM → Obsidian ─────────────────────────────────────────────────────

    async def _on_llm_completed(self, data: dict) -> None:
        """После завершения LLM — пишем задачи в Obsidian."""
        if not data or not self.client.enabled:
            return

        tasks   = data.get("tasks", [])
        prologue = data.get("prologue", "")
        trigger = data.get("trigger", "")

        if not tasks:
            return

        await self.push_tasks(tasks, prologue=prologue, trigger=trigger)

    async def push_tasks(
        self,
        tasks: list[dict],
        prologue: str = "",
        trigger: str = "",
    ) -> None:
        """
        Записать задачи в Obsidian.
        evening_summary  → дозаписать итог дня (append_evening_summary)
        остальные        → создать дейли заметку (create_daily_note, не перезаписывает)
        """
        if not tasks:
            return

        # Группируем по проекту: {project_or_None: [tasks...]}
        by_project: dict[Optional[str], list[dict]] = {}
        for task in tasks:
            proj = task.get("project") or None
            by_project.setdefault(proj, []).append(task)

        for project, proj_tasks in by_project.items():
            # Используем подпапку проекта только если она реально есть в vault.
            # Иначе — корневая Daily/ (не создаём папки чужих проектов в vault).
            vault_project: Optional[str] = None
            if project and await self.client.project_exists_in_vault(project):
                vault_project = project
            elif project:
                logger.debug(
                    "TaskSyncer: папка '%s' не найдена в vault → пишем в Daily/",
                    project,
                )

            if trigger == "evening_summary":
                path = await self.client.append_evening_summary(
                    proj_tasks, prologue=prologue, project=vault_project
                )
                if path:
                    logger.info(
                        "TaskSyncer: вечерний итог записан → '%s'", path
                    )
            else:
                path = await self.client.create_daily_note(
                    proj_tasks, prologue=prologue, project=vault_project
                )
                if path:
                    logger.info(
                        "TaskSyncer: дейли заметка → '%s' (%d задач, trigger=%s)",
                        path, len(proj_tasks), trigger,
                    )

    # ─── Obsidian → SQLite ───────────────────────────────────────────────────

    async def _on_daily_changed(self, data: dict) -> None:
        """
        Vault watcher засёк изменение в Daily/ → читаем файл(ы) → обновляем SQLite.
        Обрабатываем только .md файлы.
        """
        if not data or not self.client.enabled:
            return

        paths: list[str] = data.get("paths", [])
        for abs_path in paths:
            p = Path(abs_path)
            if p.suffix != ".md":
                continue
            await self._sync_file(abs_path)

    async def _sync_file(self, abs_path: str) -> None:
        """
        Прочитать конкретный Daily файл через client и обновить задачи в SQLite.
        Определяет project из пути: .../ProjectName/Daily/YYYY-MM-DD.md
        """
        path = Path(abs_path)
        parts = path.parts

        # Определяем project из пути: ищем папку Daily и берём что до неё
        project: Optional[str] = None
        daily_name = self.config.get("daily_folder", "Daily").lower()
        for i, part in enumerate(parts):
            if part.lower() == daily_name and i > 0:
                project = parts[i - 1]
                break

        # Читаем актуальные задачи
        obsidian_tasks = await self.client.get_today_tasks(project)
        if not obsidian_tasks:
            return

        # Обновляем только задачи с id (наши — с <!-- id:uuid -->)
        our_tasks = [t for t in obsidian_tasks if t.get("id")]
        if not our_tasks:
            return

        await context_store.update_tasks_from_obsidian(our_tasks)

        done_count  = sum(1 for t in our_tasks if t.get("done"))
        total_count = len(obsidian_tasks)  # включая пользовательские без id

        logger.info(
            "TaskSyncer: синхронизировано из '%s': %d/%d выполнено",
            path.name, done_count, len(our_tasks),
        )

        # Уведомляем шину — например, tray_app обновит счётчик
        await self.bus.emit("obsidian.tasks_updated", {
            "tasks":   our_tasks,
            "done":    done_count,
            "total":   total_count,
            "project": project,
        })

        # Все задачи выполнены — специальное событие
        all_done = all(t.get("done") for t in obsidian_tasks)
        if all_done and total_count > 0:
            await self.bus.emit("obsidian.all_done", {
                "count":   total_count,
                "project": project,
            })
            logger.info("TaskSyncer: 🎉 все %d задач выполнены (%s)!",
                        total_count, project or "общие")

    # ─── Утилиты ────────────────────────────────────────────────────────────

    async def sync_from_obsidian(self, project: Optional[str] = None) -> int:
        """
        Принудительная синхронизация из Obsidian в SQLite.
        Используется при запуске сервиса и из консольного чата.
        Возвращает число синхронизированных задач.
        """
        if not self.client.enabled:
            return 0

        obsidian_tasks = await self.client.get_today_tasks(project)
        our_tasks = [t for t in obsidian_tasks if t.get("id")]

        if our_tasks:
            await context_store.update_tasks_from_obsidian(our_tasks)
            logger.info("TaskSyncer: принудительная синхронизация: %d задач", len(our_tasks))

        return len(our_tasks)
