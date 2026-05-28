"""
errors/error_store.py — хранение, дедупликация и агрегация ошибок.

Логика спайка: если один и тот же error_type в одном file встречается
5+ раз за час → эмитит errors.spike → TriggerEngine рассматривает внеплановый LLM.

Хранит: в SQLite таблица errors, TTL 7 дней (очищается в midnight_cleanup).
"""

import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from storage import context_store

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


class ErrorStore:
    """
    Принимает события error.runtime и error.static,
    дедуплицирует, сохраняет в SQLite, следит за спайками.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        err_cfg = config.get("errors", {})
        self._spike_threshold: int   = err_cfg.get("spike_threshold", 5)
        self._spike_window_min: int  = err_cfg.get("spike_window_min", 60)
        self._debounce_sec: int      = err_cfg.get("debounce_sec", 30)
        self.bus = bus

        # Дедупликация: (error_type, file) → last_seen_ts
        self._last_seen: dict[tuple, float] = {}

        # Счётчик для спайков: (error_type, file) → [timestamps]
        self._spike_counts: dict[tuple, list[float]] = defaultdict(list)

    def subscribe(self) -> None:
        """Подписаться на события шины."""
        self.bus.on("error.runtime", self._on_error)
        self.bus.on("error.static",  self._on_static_batch)
        logger.debug("ErrorStore: подписки установлены")

    async def _on_error(self, data: dict) -> None:
        """Обработать одну runtime-ошибку."""
        if not data:
            return
        await self._process_error(data)

    async def _on_static_batch(self, data: dict) -> None:
        """Обработать батч статических ошибок от static_analyzer."""
        if not data:
            return
        file_path = data.get("file", "")
        for err in data.get("errors", []):
            error = {
                "error_type": err.get("code", "static"),
                "message":    err.get("message", ""),
                "file":       file_path,
                "line":       err.get("line"),
                "source":     "static",
                "project":    err.get("project", ""),
            }
            await self._process_error(error)

    async def _process_error(self, error: dict) -> None:
        """Дедуплицировать, сохранить, проверить спайк."""
        key = (error.get("error_type", ""), error.get("file", ""))
        now = time.time()

        # Дедупликация: одна ошибка в одном месте — не чаще раза в debounce_sec
        last = self._last_seen.get(key, 0)
        if now - last < self._debounce_sec:
            return
        self._last_seen[key] = now

        # Сохраняем в SQLite
        try:
            await context_store.save_error(error)
        except Exception as e:
            logger.error("ErrorStore: ошибка сохранения в БД — %s", e)

        logger.debug(
            "ErrorStore: %s в %s:%s",
            error.get("error_type"), error.get("file"), error.get("line")
        )

        # Проверяем спайк
        await self._check_spike(key, now, error)

    async def _check_spike(self, key: tuple, now: float, error: dict) -> None:
        """
        Если error_type+file встречается >=spike_threshold раз за spike_window_min →
        эмитит errors.spike и сбрасывает счётчик.
        """
        window_sec = self._spike_window_min * 60
        timestamps = self._spike_counts[key]
        timestamps.append(now)

        # Убираем устаревшие
        self._spike_counts[key] = [t for t in timestamps if now - t <= window_sec]
        count = len(self._spike_counts[key])

        if count >= self._spike_threshold:
            error_type, file_path = key
            logger.warning(
                "ErrorStore: спайк! %s в %s — %d раз за %d мин",
                error_type, file_path, count, self._spike_window_min,
            )
            await self.bus.emit("errors.spike", {
                "type":    error_type,
                "file":    file_path,
                "count":   count,
                "project": error.get("project", ""),
            })
            # Сбрасываем чтобы не спамить
            self._spike_counts[key] = []

    async def get_recent(self, hours: int = 24) -> list[dict]:
        """Ошибки за последние N часов из SQLite."""
        return await context_store.get_errors(hours=hours)
