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
import uuid
from datetime import date
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from storage import context_store

if TYPE_CHECKING:
    from obsidian.client import ObsidianClient
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


def _parse_undone_tasks(content: str) -> list[dict]:
    """
    Парсит невыполненные [ ] задачи из markdown.
    Возвращает список задач с title, id, priority (угадывается из секции).
    """
    tasks = []
    current_priority = "MED"
    _PRIORITY_MAP = {"🔴": "HIGH", "🟡": "MED", "🟢": "LOW"}

    for line in content.splitlines():
        for emoji, prio in _PRIORITY_MAP.items():
            if emoji in line and line.startswith("##"):
                current_priority = prio
                break
        m = re.match(
            r'\s*-\s+\[ \]\s+(.+?)(?:\s+<!--\s+id:([^>]+?)\s+-->)?\s*$',
            line,
        )
        if m:
            tasks.append({
                "title":    m.group(1).strip(),
                "id":       (m.group(2) or "").strip(),
                "priority": current_priority,
                "done":     False,
            })
    return tasks


def _parse_done_state(content: str) -> dict[str, dict]:
    """
    Парсит выполненные задачи из markdown.
    Возвращает: {task_id: {"done_by": str, "title": str}} для всех [x] задач.
    Ключ — id если есть, иначе нормализованный заголовок.
    Сохраняет атрибуцию ассистента если есть <!-- ✓ ... -->.
    """
    result = {}
    for line in content.splitlines():
        m = re.match(
            r'\s*-\s+\[x\]\s+(.+?)(?:\s+<!--\s+id:([^>]+?)\s+-->)?'
            r'(?:\s+<!--\s+✓\s+([^>]+?)\s+-->)?\s*$',
            line,
        )
        if m:
            title   = m.group(1).strip()
            task_id = (m.group(2) or "").strip()
            done_by = (m.group(3) or "").strip()
            key = task_id if task_id else _norm_title(title)
            result[key] = {"done_by": done_by, "title": title}
    return result


def _norm_title(title: str) -> str:
    """Нормализованный заголовок для fuzzy-сравнения (lowercase + trim)."""
    return re.sub(r'\s+', ' ', title.lower().strip())

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
        self.bus.on("trigger.rollover",         self._on_rollover)

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
        остальные        → smart merge + перезапись (сохраняем [x] задачи)
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
                # Smart merge: сохраняем [x] задачи, перезаписываем файл
                if self.client.shared_root:
                    path = await self._smart_write_daily_fs(
                        proj_tasks, prologue, project, open_after=True
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

    # ─── Общий хелпер: smart merge + запись в ФС ────────────────────────────

    async def _smart_write_daily_fs(
        self,
        tasks: list[dict],
        prologue: str,
        project: Optional[str],
        *,
        open_after: bool = False,
    ) -> Optional[str]:
        """
        Smart merge + FS write дейли заметки.
        Читает существующий файл, сохраняет [x] задачи, перезаписывает файл новым контентом.
        open_after=True — открыть Obsidian после записи (для активного проекта).
        open_after=False — тихая запись (для фоновых проектов).
        """
        if not self.client.shared_root:
            return None

        fs_path = self.client.daily_path_fs(project)

        done_state: dict[str, dict] = {}
        if fs_path.exists():
            try:
                existing = await asyncio.to_thread(fs_path.read_text, "utf-8")
                done_state = _parse_done_state(existing)
                if done_state:
                    logger.debug(
                        "TaskSyncer: '%s' — сохраняем %d выполненных задач",
                        project or "общие", len(done_state)
                    )
            except Exception as e:
                logger.debug("TaskSyncer: не удалось прочитать существующий файл: %s", e)

        # Строим индекс по нормализованным заголовкам для fuzzy-match
        done_by_title = {
            _norm_title(v.get("title", k)): v
            for k, v in done_state.items()
        }

        for task in tasks:
            tid   = task.get("id", "")
            title = task.get("title", "")
            match = done_state.get(tid) or done_by_title.get(_norm_title(title))
            if match:
                task["done"] = True
                task["done_by"] = match.get("done_by", "")

        # Сохраняем незакрытые [ ] задачи из существующего файла
        # которые не совпадают с новыми от LLM (перенос с прошлого дня)
        if fs_path.exists():
            existing_undone = _parse_undone_tasks(
                await asyncio.to_thread(fs_path.read_text, "utf-8")
            )
            new_titles = {_norm_title(t.get("title", "")) for t in tasks}
            carried = [
                t for t in existing_undone
                if _norm_title(t.get("title", "")) not in new_titles
            ]
            if carried:
                tasks = carried + tasks
                logger.debug(
                    "TaskSyncer: перенесено %d незакрытых задач с предыдущего дня",
                    len(carried),
                )

        content = self.client._build_task_list(tasks, prologue, project)

        def _write():
            fs_path.parent.mkdir(parents=True, exist_ok=True)
            fs_path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)

        done_count = sum(1 for t in tasks if t.get("done"))
        logger.info(
            "TaskSyncer: '%s' → %d задач (%d выполнено сохранено)%s",
            project or "общие", len(tasks), done_count,
            "" if open_after else " (тихая запись)",
        )

        if open_after and self.client.auto_open:
            from core.notifier import open_obsidian_note
            asyncio.create_task(open_obsidian_note(fs_path))

        return str(fs_path)

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

        path = await self._smart_write_daily_fs(
            tasks, prologue, project, open_after=False
        )
        if path:
            logger.info(
                "TaskSyncer[bg]: '%s' → без уведомления", project
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

    # ─── Перенос задач на новый день (rollover) ─────────────────────────────

    async def _on_rollover(self, data: dict) -> None:
        """Обработчик события trigger.rollover — переносит задачи активного проекта."""
        project = (data or {}).get("project")
        if not project or not self.client.shared_root:
            return
        count = await self.rollover_to_today(project)
        if count:
            logger.info("TaskSyncer: rollover %d задач → сегодня (%s)", count, project)

    async def rollover_to_today(self, project: str) -> int:
        """
        Перенести незакрытые задачи из последнего дейли файла в сегодняшний.
        Запускается при смене даты (старт сервиса, утренний брифинг).
        Не трогает файл если сегодняшний уже существует.
        Возвращает число перенесённых задач.
        """
        if not self.client.shared_root or not project:
            return 0

        today_path = self.client.daily_path_fs(project)
        if today_path.exists():
            return 0  # сегодняшний файл уже есть — LLM сделает своё дело

        daily_dir = self.client.shared_root / project / self.client.daily_folder
        if not daily_dir.exists():
            return 0

        # Находим самый свежий daily файл (кроме сегодняшнего)
        today_str = date.today().strftime("%d.%m.%Y")
        md_files = sorted(
            [f for f in daily_dir.glob("*.md") if f.stem != today_str],
            reverse=True,
        )
        if not md_files:
            return 0

        prev_path = md_files[0]
        try:
            content = await asyncio.to_thread(prev_path.read_text, "utf-8")
        except Exception as e:
            logger.debug("TaskSyncer[rollover]: не удалось прочитать %s: %s", prev_path, e)
            return 0

        # Парсим незакрытые задачи с сохранением приоритета из секций
        tasks: list[dict] = []
        current_priority = "MED"
        _PRIORITY_MAP = {"🔴": "HIGH", "🟡": "MED", "🟢": "LOW"}

        for line in content.splitlines():
            # Обновляем приоритет из заголовка секции
            for emoji, prio in _PRIORITY_MAP.items():
                if emoji in line and line.startswith("##"):
                    current_priority = prio
                    break

            # Незакрытые задачи с id
            m = re.match(
                r'\s*-\s+\[ \]\s+(.+?)(?:\s+<!--\s+id:([^>]+?)\s+-->)?\s*$',
                line,
            )
            if m:
                title   = m.group(1).strip()
                task_id = (m.group(2) or str(uuid.uuid4())).strip()
                tasks.append({
                    "id":       task_id,
                    "title":    title,
                    "priority": current_priority,
                    "done":     False,
                    "project":  project,
                })

        if not tasks:
            logger.debug("TaskSyncer[rollover]: '%s' — нет незакрытых задач в %s",
                         project, prev_path.name)
            return 0

        prev_date = prev_path.stem  # "27.05.2026"
        prologue = f"Перенесено с {prev_date} · жди обновления от ассистента"

        await self._smart_write_daily_fs(tasks, prologue, project, open_after=False)
        logger.info(
            "TaskSyncer[rollover]: %d задач %s → %s",
            len(tasks), prev_path.name, today_path.name,
        )
        return len(tasks)

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
