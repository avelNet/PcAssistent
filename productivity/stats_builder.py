"""
productivity/stats_builder.py — итоговая статистика дня для вечернего брифинга.

Собирает данные из:
  - session_tracker (фокус, переключения)
  - context_store (задачи, ошибки)
  - git_watcher (коммиты)

Возвращает словарь для prompt_engine.
"""

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from productivity.session_tracker import SessionTracker
    from collectors.git_watcher import GitWatcher
    from storage.context_store import ContextStore


logger = logging.getLogger(__name__)


class StatsBuilder:
    """
    Агрегирует статистику дня из нескольких источников.
    Используется TriggerEngine для evening_summary.
    """

    def __init__(
        self,
        session_tracker: "SessionTracker",
        git_watcher: Optional["GitWatcher"] = None,
    ):
        self._session = session_tracker
        self._git = git_watcher

    async def build(self) -> dict:
        """
        Собрать полную статистику дня.
        Возвращает словарь который передаётся в prompt_engine.
        """
        from storage import context_store

        stats = self._session.get_stats()

        # Задачи из SQLite
        today_tasks = await context_store.get_tasks_for_date()
        done_tasks    = [t for t in today_tasks if t.get("done")]
        pending_tasks = [t for t in today_tasks if not t.get("done")]

        # Ошибки за сегодня
        errors = await context_store.get_errors(hours=24)
        error_types = {}
        for e in errors:
            et = e.get("error_type", "unknown")
            error_types[et] = error_types.get(et, 0) + 1

        # Repeat ошибки (встречались > 1 раза)
        repeat_errors = [
            f"{etype} ({cnt}x)" for etype, cnt in error_types.items() if cnt > 1
        ]

        # Git-статистика
        commits_today = 0
        most_edited_file = ""
        if self._git:
            try:
                snapshots = await self._git.get_all_snapshots()
                for snap in snapshots:
                    commits_today += snap.get("commits_today", 0)
                    # Берём самый часто упоминаемый файл
                    changed = snap.get("changed_files", [])
                    if changed and not most_edited_file:
                        most_edited_file = changed[0] if isinstance(changed[0], str) else ""
            except Exception as e:
                logger.debug("StatsBuilder: ошибка git_watcher — %s", e)

        # Конвертируем минуты в часы где нужно
        deep_work_hours = round(stats.get("deep_work_minutes", 0) / 60, 1)

        result = {
            "deep_work_hours":   deep_work_hours,
            "shallow_work_min":  stats.get("shallow_work_minutes", 0),
            "browser_min":       stats.get("browser_minutes", 0),
            "distraction_min":   stats.get("distraction_minutes", 0),
            "switches":          stats.get("switches_count", 0),
            "avg_focus_min":     stats.get("avg_focus_duration", 0),
            "session_dur_min":   stats.get("session_duration_min", 0),
            "commits":           commits_today,
            "errors_total":      len(errors),
            "static_errors":     sum(1 for e in errors if e.get("source") == "static"),
            "repeat_errors":     repeat_errors[:5],
            "tasks_done":        len(done_tasks),
            "tasks_total":       len(today_tasks),
            "tasks_pending":     [t.get("title", "") for t in pending_tasks[:5]],
            "most_edited_file":  most_edited_file,
        }

        logger.debug(
            "StatsBuilder: deep=%.1fч, коммитов=%d, задач=%d/%d",
            deep_work_hours, commits_today, len(done_tasks), len(today_tasks),
        )
        return result
