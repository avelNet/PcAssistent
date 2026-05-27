"""
collectors/fs_watcher.py — inotify-наблюдатель за рабочими директориями.

Батчинг: копит события 5 секунд, потом одним emit fs.batch.
Игнорирует: .git/, __pycache__/, node_modules/, .venv/, target/, .idea/,
            *.pyc, *.swp, *.tmp, *~
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "target", ".idea", ".tox", "dist", "build", ".mypy_cache",
}
_IGNORE_EXTS = {".pyc", ".pyo", ".swp", ".tmp", ".swo"}
_IGNORE_SUFFIXES = ("~",)


def _should_ignore(path: str) -> bool:
    p = Path(path)
    # Игнорируем служебные папки
    for part in p.parts:
        if part in _IGNORE_DIRS:
            return True
    # Игнорируем временные файлы
    if p.suffix in _IGNORE_EXTS:
        return True
    if p.name.endswith(_IGNORE_SUFFIXES):
        return True
    return False


class _BatchHandler(FileSystemEventHandler):
    """Собирает события в батч, передаёт в очередь."""

    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self._queue = queue

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        if _should_ignore(src):
            return

        item = {
            "type": event.event_type,      # created / modified / deleted / moved
            "path": src,
            "ext":  Path(src).suffix,
            "ts":   time.time(),
        }

        # Неблокирующая put_nowait — если очередь полна, пропускаем событие
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


class FSWatcher:
    """
    Watchdog inotify → батч-эмиттер.

    Запускается в фоновом потоке (watchdog Observer).
    Каждые BATCH_SEC секунд собирает накопленные события и эмитит fs.batch.
    """

    BATCH_SEC = 5

    def __init__(self, config: dict, bus: "EventBus"):
        self.config = config.get("collectors", {}).get("filesystem", {})
        self.bus = bus
        self._observer: Observer | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    async def start(self) -> None:
        watch_dirs = [
            Path(d).expanduser()
            for d in self.config.get("watch_dirs", [])
        ]

        if not watch_dirs:
            logger.debug("FSWatcher: нет watch_dirs в конфиге — пропускаем")
            return

        handler = _BatchHandler(self._queue)
        self._observer = Observer()

        watched = 0
        for d in watch_dirs:
            if d.exists():
                self._observer.schedule(handler, str(d), recursive=True)
                watched += 1
            else:
                logger.debug("FSWatcher: директория не существует: %s", d)

        if watched == 0:
            logger.debug("FSWatcher: ни одна watch_dir не найдена — пропускаем")
            return

        self._observer.start()
        self._task = asyncio.create_task(self._batch_loop(), name="fs_watcher_batch")
        logger.info("FSWatcher: запущен, слежу за %d директориями", watched)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)
        if self._task:
            self._task.cancel()
        logger.info("FSWatcher: остановлен")

    async def _batch_loop(self) -> None:
        """Каждые BATCH_SEC секунд собирает очередь и эмитит одно событие."""
        while True:
            try:
                await asyncio.sleep(self.BATCH_SEC)

                batch: list[dict] = []
                while not self._queue.empty():
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    # Дедупликация: если один файл изменился много раз — берём последнее
                    seen: dict[str, dict] = {}
                    for ev in batch:
                        seen[ev["path"]] = ev
                    unique_batch = list(seen.values())

                    await self.bus.emit("fs.batch", {"events": unique_batch})
                    logger.debug("FSWatcher: эмитит %d событий", len(unique_batch))

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("FSWatcher: ошибка в batch_loop")
                await asyncio.sleep(10)
