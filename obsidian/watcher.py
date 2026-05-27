"""
watcher.py — inotify на весь Obsidian root.

Два типа событий:
  1. Изменение в Daily/ → obsidian.tasks_updated (синхронизация задач)
  2. Изменение в любом другом месте → obsidian.vault_changed (обновление контекста)

Новые папки (новые проекты) подхватываются автоматически — watchdog
рекурсивно следит за root.
"""

import asyncio
import logging
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.event_bus import EventBus

logger = logging.getLogger(__name__)


class _VaultEventHandler(FileSystemEventHandler):
    def __init__(self, root: Path, daily_folder: str,
                 loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        super().__init__()
        self.root = root
        self.daily_folder = daily_folder.lower()
        self.loop = loop
        self.queue = queue

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def _handle(self, event: FileSystemEvent) -> None:
        path = Path(event.src_path)

        # Игнорируем служебные файлы
        if any(part.startswith(".") for part in path.parts):
            return
        if path.suffix not in (".md", ""):
            return
        if event.is_directory:
            # Новая папка = новый проект, логируем
            logger.info("VaultWatcher: новая папка обнаружена: %s", path.name)

        # Определяем тип события по расположению файла
        try:
            parts = path.relative_to(self.root).parts
            # parts[0] — vault или папка проекта, parts[1] — подпапка
            is_daily = any(p.lower() == self.daily_folder for p in parts)
        except ValueError:
            return

        event_type = "daily" if is_daily else "vault"
        self.loop.call_soon_threadsafe(
            self.queue.put_nowait, (event_type, str(path))
        )


class ObsidianWatcher:
    def __init__(self, config: dict, bus: EventBus):
        obs_cfg = config.get("obsidian", {})
        self.root = Path(obs_cfg.get("root", "~/Obsidian")).expanduser()
        self.daily_folder = obs_cfg.get("daily_folder", "Daily")
        self.bus = bus
        self._observer = Observer()
        self._queue: asyncio.Queue = asyncio.Queue()

    async def start(self) -> None:
        if not self.root.exists():
            logger.warning("ObsidianWatcher: папка не найдена: %s — пропускаем", self.root)
            return

        loop = asyncio.get_running_loop()
        handler = _VaultEventHandler(self.root, self.daily_folder, loop, self._queue)

        # recursive=True — подхватывает новые вложенные папки автоматически
        self._observer.schedule(handler, str(self.root), recursive=True)
        await asyncio.to_thread(self._observer.start)

        asyncio.create_task(self._process_queue(), name="obsidian_watcher:process")
        logger.info("ObsidianWatcher: слежу за %s (recursive)", self.root)

    def stop(self) -> None:
        self._observer.stop()

    async def _process_queue(self) -> None:
        """Дебаунс 1 сек, затем emit события."""
        pending: dict[str, set[str]] = {"daily": set(), "vault": set()}

        while True:
            try:
                event_type, path = await self._queue.get()
                pending[event_type].add(path)

                # Дренируем очередь 1 секунду
                deadline = asyncio.get_event_loop().time() + 1.0
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        et, p = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                        pending[et].add(p)
                    except asyncio.TimeoutError:
                        break

                # Эмитим накопленные события
                if pending["daily"]:
                    await self.bus.emit("obsidian.daily_changed", {
                        "paths": list(pending["daily"])
                    })
                    pending["daily"].clear()

                if pending["vault"]:
                    await self.bus.emit("obsidian.vault_changed", {
                        "paths": list(pending["vault"])
                    })
                    pending["vault"].clear()

            except Exception:
                logger.exception("ObsidianWatcher: ошибка в _process_queue")
