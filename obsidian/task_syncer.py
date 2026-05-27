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
import re
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from storage import context_store

if TYPE_CHECKING:
    from obsidian.client import ObsidianClient
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


def _parse_done_state(content: str) -> dict[str, dict]:
    """
    Парсит выполненные задачи из markdown.
    Возвращает: {task_id: {"done_by": str}} для всех [x] задач с id.
    Сохраняет и атрибуцию ассистента если есть <!-- ✓ ... -->.
    """
    result = {}
    for line in content.splitlines():
        m = re.match(
            r'\s*-\s+\[x\]\s+.+<!--\s+id:([^>]+?)\s+-->'
            r'(?:\s+<!--\s+✓\s+([^>]+?)\s+-->)?',
            line,
        )
        if m:
            task_id = m.group(1).strip()
            done_by = (m.group(2) or "").strip()
            result[task_id] = {"done_by": done_by}
    return result

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
        self.bus.on("llm.completed",            self._on_llm_completed)
        self.bus.on("llm.background_completed", self._on_background_completed)
        self.bus.on("obsidian.daily_changed",   self._on_daily_changed)

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
            if trigger == "evening_summary":
                path = await self.client.append_evening_summary(
                    proj_tasks, prologue=prologue, project=project
                )
                if path:
                    logger.info(
                        "TaskSyncer: вечерний итог записан → '%s'", path
                    )
            else:
                path = await self.client.create_daily_note(
                    proj_tasks, prologue=prologue, project=project
                )
                if path:
                    logger.info(
                        "TaskSyncer: дейли заметка → '%s' (%d задач, trigger=%s)",
                        path, len(proj_tasks), trigger,
                    )

    # ─── Фоновая запись (другие проекты) ────────────────────────────────────

    async def _on_background_completed(self, data: dict) -> None:
        """
        Тихая запись задач для фонового проекта.
        Без уведомлений, без открытия Obsidian — только файл.
        Сохраняет уже выполненные [x] задачи если они были отмечены ранее.
        """
        if not data or not self.client.enabled:
            return

        tasks    = data.get("tasks", [])
        project  = data.get("project")
        prologue = data.get("prologue", "")

        if not tasks or not project:
            return

        if not self.client.shared_root:
            return

        fs_path = self.client.daily_path_fs(project)

        # ── Слияние: сохраняем выполненные задачи из существующего файла ──
        # Если пользователь (или ассистент) уже отметил задачу [x] — не теряем.
        done_state: dict[str, dict] = {}
        if fs_path.exists():
            try:
                existing = await asyncio.to_thread(fs_path.read_text, "utf-8")
                done_state = _parse_done_state(existing)
                if done_state:
                    logger.debug(
                        "TaskSyncer[bg]: '%s' — сохраняем %d выполненных задач",
                        project, len(done_state)
                    )
            except Exception as e:
                logger.debug("TaskSyncer[bg]: не удалось прочитать существующий файл: %s", e)

        # Применяем preserved done-статус к задачам от LLM
        if done_state:
            for task in tasks:
                tid = task.get("id", "")
                if tid in done_state:
                    task["done"] = True
                    task["done_by"] = done_state[tid].get("done_by", "")

        content = self.client._build_task_list(tasks, prologue, project)

        def _write():
            fs_path.parent.mkdir(parents=True, exist_ok=True)
            fs_path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        done_count = sum(1 for t in tasks if t.get("done"))
        logger.info(
            "TaskSyncer[bg]: '%s' → %d задач (%d выполнено сохранено, без уведомления)",
            project, len(tasks), done_count,
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

        try:
            obsidian_tasks = await self.client.get_today_tasks(project)
        except Exception as e:
            logger.debug("TaskSyncer: sync_from_obsidian ошибка чтения: %s", e)
            return 0

        our_tasks = [t for t in obsidian_tasks if t.get("id")]

        if our_tasks:
            await context_store.update_tasks_from_obsidian(our_tasks)
            logger.info("TaskSyncer: принудительная синхронизация: %d задач", len(our_tasks))

        return len(our_tasks)
