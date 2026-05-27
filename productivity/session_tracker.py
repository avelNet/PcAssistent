"""
productivity/session_tracker.py — накапливает события от focus_analyzer,
строит метрики продуктивности.

Метрики:
  deep_work_minutes     — непрерывная работа в IDE > 15 мин
  shallow_work_minutes  — работа с частыми переключениями
  browser_minutes       — время в браузере
  distraction_minutes   — YouTube, мессенджеры
  switches_count        — количество переключений окон за день
  avg_focus_duration    — среднее время непрерывной работы

"Глубокая работа": IDE в фокусе непрерывно > deep_work_threshold_min без
переключения в browser/distraction.

Хранит в памяти для текущей сессии; stats_builder читает напрямую.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


class SessionTracker:
    """
    Слушает focus.changed и строит метрики текущей рабочей сессии.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        prod_cfg = config.get("productivity", {})
        self._deep_work_threshold: int = prod_cfg.get("deep_work_threshold_min", 15)
        self.bus = bus

        # Накопленные минуты по категориям
        self._minutes: dict[str, float] = {
            "deep_work":   0.0,
            "shallow_work": 0.0,
            "browser":     0.0,
            "distraction": 0.0,
            "other":       0.0,
        }
        self._switches_count: int = 0

        # Текущий "deep work streak" в минутах
        self._deep_streak_min: float = 0.0
        self._last_category:   str   = ""
        self._session_start:   float = time.time()

        # Список продолжительностей непрерывных фокусировок (для avg)
        self._focus_durations: list[float] = []

    def subscribe(self) -> None:
        self.bus.on("focus.changed", self._on_focus_changed)
        logger.debug("SessionTracker: подписки установлены")

    async def _on_focus_changed(self, data: dict) -> None:
        if not data:
            return

        from_cat   = data.get("from_cat", "other")
        to_cat     = data.get("to_cat", "other")
        duration_s = data.get("duration_sec", 0)
        duration_m = duration_s / 60.0

        self._switches_count += 1
        self._focus_durations.append(duration_m)

        # Засчитываем время предыдущего фокуса
        if from_cat == "deep_work":
            if duration_m >= self._deep_work_threshold:
                self._minutes["deep_work"] += duration_m
                self._deep_streak_min += duration_m
                logger.debug("SessionTracker: deep_work +%.1f мин (streak=%.1f)", duration_m, self._deep_streak_min)
            else:
                self._minutes["shallow_work"] += duration_m
                self._deep_streak_min = 0.0
        elif from_cat == "browser":
            self._minutes["browser"] += duration_m
            self._deep_streak_min = 0.0
        elif from_cat == "distraction":
            self._minutes["distraction"] += duration_m
            self._deep_streak_min = 0.0
        else:
            self._minutes["other"] += duration_m

        self._last_category = to_cat

    def get_stats(self) -> dict:
        """Вернуть текущие метрики сессии."""
        total_focus = self._minutes["deep_work"] + self._minutes["shallow_work"]
        avg_focus = (
            sum(self._focus_durations) / len(self._focus_durations)
            if self._focus_durations else 0.0
        )

        session_duration = (time.time() - self._session_start) / 60.0

        return {
            "deep_work_minutes":    round(self._minutes["deep_work"], 1),
            "shallow_work_minutes": round(self._minutes["shallow_work"], 1),
            "browser_minutes":      round(self._minutes["browser"], 1),
            "distraction_minutes":  round(self._minutes["distraction"], 1),
            "switches_count":       self._switches_count,
            "avg_focus_duration":   round(avg_focus, 1),
            "session_duration_min": round(session_duration, 1),
            "deep_streak_min":      round(self._deep_streak_min, 1),
        }

    def reset(self) -> None:
        """Сбросить статистику (новый день / новая сессия)."""
        for k in self._minutes:
            self._minutes[k] = 0.0
        self._switches_count = 0
        self._deep_streak_min = 0.0
        self._focus_durations.clear()
        self._session_start = time.time()
