"""
EventBus — центральная шина событий.
Все модули общаются только через неё, прямых зависимостей между модулями нет.

Использование:
    bus = EventBus()
    bus.on("git.changed", my_handler)
    await bus.emit("git.changed", {"repo": "...", "snapshot": {...}})
"""

import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        """Подписаться на событие."""
        self._handlers[event].append(handler)
        logger.debug("Подписка: %s → '%s'", handler.__qualname__, event)

    def off(self, event: str, handler: Callable) -> None:
        """Отписаться от события."""
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass

    async def emit(self, event: str, data: Any = None) -> None:
        """
        Опубликовать событие.
        - async handler → создаётся asyncio.Task (fire-and-forget)
        - sync handler  → вызывается напрямую
        """
        handlers = list(self._handlers.get(event, []))
        if not handlers:
            logger.debug("Событие '%s' — нет подписчиков", event)
            return

        logger.debug("Событие '%s' → %d обработчиков", event, len(handlers))

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    asyncio.create_task(
                        handler(data),
                        name=f"event:{event}:{handler.__qualname__}",
                    )
                else:
                    handler(data)
            except Exception:
                logger.exception(
                    "Ошибка в обработчике '%s' для события '%s'",
                    handler.__qualname__,
                    event,
                )
