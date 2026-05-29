"""
obsidian/progress_tracker.py — статистика выполнения задач за 7 дней.

Что выявляет:
  - Средний % выполнения по дням
  - Задачи которые появляются > 2 дней подряд (зависшие)
  - Проекты с 0% выполнением (заброшенные)

LLM использует это чтобы не предлагать заведомо невыполнимые задачи
и повышать приоритет зависших (§3.22).
"""

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from storage import context_store

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ProgressTracker:
    """
    Читает историю задач из SQLite и строит аналитику за 7 дней.
    Вызывается из context_builder перед каждым LLM-запросом.
    """

    def __init__(self, days: int = 7):
        self._days = days

    async def build(self) -> dict:
        """
        Собрать аналитику прогресса.
        Возвращает словарь для prompt_engine.
        """
        tasks = await context_store.get_task_history(days=self._days)

        if not tasks:
            return {}

        # Группируем по дате
        by_date: dict[str, list[dict]] = defaultdict(list)
        for t in tasks:
            d = t.get("date", "")
            if d:
                by_date[d].append(t)

        # % выполнения по дням
        completion_by_day: dict[str, float] = {}
        for d, day_tasks in by_date.items():
            if day_tasks:
                done = sum(1 for t in day_tasks if t.get("done"))
                completion_by_day[d] = round(done / len(day_tasks) * 100, 1)

        avg_completion = (
            round(sum(completion_by_day.values()) / len(completion_by_day), 1)
            if completion_by_day else 0.0
        )

        # Зависшие задачи: заголовок встречается > 2 дней подряд и не выполнен
        stuck_tasks = self._find_stuck_tasks(by_date)

        # Заброшенные проекты: 0% за последние 3+ дней
        abandoned_projects = self._find_abandoned_projects(by_date)

        # Самый продуктивный день
        best_day = max(completion_by_day, key=completion_by_day.get) if completion_by_day else ""

        chronic_errors = await self._find_chronic_errors()

        result = {
            "avg_completion_pct":  avg_completion,
            "completion_by_day":   completion_by_day,
            "stuck_tasks":         stuck_tasks[:5],
            "abandoned_projects":  abandoned_projects[:3],
            "chronic_errors":      chronic_errors[:3],
            "best_day":            best_day,
            "best_day_pct":        completion_by_day.get(best_day, 0),
            "days_analyzed":       len(by_date),
        }

        logger.debug(
            "ProgressTracker: %d дней, avg=%.1f%%, зависших=%d",
            len(by_date), avg_completion, len(stuck_tasks),
        )
        return result

    def _find_stuck_tasks(self, by_date: dict) -> list[dict]:
        """
        Найти задачи которые появляются > 2 дней подряд без выполнения.
        Возвращает: [{ title, days_count, project }]
        """
        # title → [{date, done, project}]
        title_history: dict[str, list[dict]] = defaultdict(list)
        for d, tasks in sorted(by_date.items()):
            for t in tasks:
                title = t.get("title", "").strip()
                if title:
                    title_history[title].append({
                        "date":    d,
                        "done":    t.get("done", False),
                        "project": t.get("project", ""),
                    })

        stuck = []
        for title, history in title_history.items():
            # Берём только невыполненные появления
            pending_days = [h for h in history if not h.get("done")]
            if len(pending_days) >= 3:
                stuck.append({
                    "title":      title,
                    "days_count": len(pending_days),
                    "project":    pending_days[-1].get("project", ""),
                    "first_seen": pending_days[0].get("date", ""),
                })

        return sorted(stuck, key=lambda x: x["days_count"], reverse=True)

    async def _find_chronic_errors(self) -> list[dict]:
        """
        Ошибки повторяющиеся 3+ дня подряд → хроническая проблема.
        Возвращает: [{ error_type, file, days_count }]
        """
        errors = await context_store.get_errors(hours=24 * self._days)
        if not errors:
            return []

        from datetime import datetime, timezone
        from collections import defaultdict

        # Группируем по (error_type, file) и дате
        by_key: dict[tuple, set] = defaultdict(set)
        for e in errors:
            key = (e.get("error_type", ""), e.get("file", ""))
            ts = e.get("timestamp") or e.get("created_at") or ""
            try:
                day = str(ts)[:10]  # YYYY-MM-DD
                if day:
                    by_key[key].add(day)
            except Exception:
                pass

        chronic = []
        for (etype, fpath), days in by_key.items():
            if len(days) >= 3:
                chronic.append({
                    "error_type": etype,
                    "file":       fpath,
                    "days_count": len(days),
                })

        return sorted(chronic, key=lambda x: x["days_count"], reverse=True)

    def _find_abandoned_projects(self, by_date: dict) -> list[str]:
        """
        Найти проекты с 0% выполнением за последние 3+ дней.
        """
        # project → [completion_pct]
        project_completions: dict[str, list[float]] = defaultdict(list)

        # Берём только последние 3 дня
        recent_dates = sorted(by_date.keys())[-3:]

        for d in recent_dates:
            # Группируем задачи по проекту
            proj_tasks: dict[str, list] = defaultdict(list)
            for t in by_date[d]:
                proj = t.get("project") or "общие"
                proj_tasks[proj].append(t)

            for proj, tasks in proj_tasks.items():
                done = sum(1 for t in tasks if t.get("done"))
                pct = done / len(tasks) * 100 if tasks else 0
                project_completions[proj].append(pct)

        abandoned = []
        for proj, pcts in project_completions.items():
            # Если за все 3 дня 0% — проект заброшен
            if len(pcts) >= 2 and all(p == 0 for p in pcts):
                abandoned.append(proj)

        return abandoned
