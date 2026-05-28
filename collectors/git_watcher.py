"""
git_watcher.py — следит за git-репозиториями через inotify.
Нулевая нагрузка: реагирует только на реальные события ядра.

Что отслеживает:
  .git/COMMIT_EDITMSG — новый коммит
  .git/HEAD           — смена ветки
  .git/index          — изменения в staging area
"""

import asyncio
import logging
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from core.event_bus import EventBus
from storage import context_store

logger = logging.getLogger(__name__)

# Файлы внутри .git/, изменение которых нас интересует
_WATCHED_FILES = {"COMMIT_EDITMSG", "HEAD", "index"}


class _GitHandler(FileSystemEventHandler):
    """Watchdog-обработчик для одного репозитория."""

    def __init__(self, repo_path: Path, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        super().__init__()
        self.repo_path = repo_path
        self.loop = loop
        self.queue = queue

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event)

    def _handle(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        filename = Path(event.src_path).name
        if filename in _WATCHED_FILES:
            logger.debug("git event: %s в %s", filename, self.repo_path.name)
            # Передаём путь репо в asyncio очередь из watchdog-потока
            self.loop.call_soon_threadsafe(self.queue.put_nowait, self.repo_path)


class GitWatcher:
    """
    Следит за всеми git-репозиториями из config.collectors.git.scan_dirs.
    При изменении — собирает снапшот и эмитит git.changed.
    """

    def __init__(self, config: dict, bus: EventBus):
        self.config = config.get("collectors", {}).get("git", {})
        self.bus = bus
        self._observer = Observer()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._repos: list[Path] = []

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._repos = await asyncio.to_thread(self._scan_repos)

        if not self._repos:
            logger.warning("GitWatcher: репозитории не найдены. Проверь collectors.git.scan_dirs в config.yaml")
            return

        logger.info("GitWatcher: найдено %d репозиториев", len(self._repos))

        for repo in self._repos:
            git_dir = repo / ".git"
            handler = _GitHandler(repo, self._loop, self._queue)
            self._observer.schedule(handler, str(git_dir), recursive=False)
            logger.debug("  watching: %s", repo)

        await asyncio.to_thread(self._observer.start)
        asyncio.create_task(self._process_queue(), name="git_watcher:process")
        logger.info("GitWatcher запущен")

    def stop(self) -> None:
        self._observer.stop()
        logger.info("GitWatcher остановлен")

    def _scan_repos(self) -> list[Path]:
        """Найти git-репозитории в scan_dirs (глубина max_depth)."""
        scan_dirs = self.config.get("scan_dirs", ["~/projects"])
        max_depth = self.config.get("max_depth", 3)
        max_repos = self.config.get("max_repos", 30)
        repos: list[Path] = []

        for scan_dir in scan_dirs:
            base = Path(scan_dir).expanduser()
            if not base.exists():
                logger.debug("scan_dir не существует: %s", base)
                continue
            self._find_repos(base, 0, max_depth, repos, max_repos)

        return repos

    def _find_repos(self, path: Path, depth: int, max_depth: int,
                    result: list[Path], max_repos: int) -> None:
        if len(result) >= max_repos:
            return
        if depth > max_depth:
            return

        if (path / ".git").is_dir():
            result.append(path)
            return  # не ищем вложенные репо

        try:
            for child in path.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    self._find_repos(child, depth + 1, max_depth, result, max_repos)
        except PermissionError:
            pass

    async def _process_queue(self) -> None:
        """
        Читает события из очереди с дебаунсингом.
        Если за 2 секунды пришло несколько событий для одного репо — обрабатываем один раз.
        """
        debounce = self.config.get("debounce_sec", 2)
        pending: set[Path] = set()

        while True:
            try:
                # Ждём первое событие
                repo = await self._queue.get()
                pending.add(repo)

                # Дренируем очередь в течение debounce секунд
                deadline = asyncio.get_event_loop().time() + debounce
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        extra = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                        pending.add(extra)
                    except asyncio.TimeoutError:
                        break

                # Обрабатываем все накопившиеся репо
                for repo_path in list(pending):
                    await self._handle_repo_changed(repo_path)
                pending.clear()

            except Exception:
                logger.exception("GitWatcher: ошибка в _process_queue")

    async def _handle_repo_changed(self, repo_path: Path) -> None:
        """Собрать снапшот репозитория и эмитить событие."""
        try:
            snapshot = await asyncio.to_thread(self._collect_snapshot, repo_path)
            await context_store.save_context("git", {"repo": str(repo_path), **snapshot})
            await self.bus.emit("git.changed", {"repo": str(repo_path), "snapshot": snapshot})
            logger.info("git.changed: %s [%s] %d незакоммиченных",
                        repo_path.name, snapshot.get("branch", "?"),
                        snapshot.get("uncommitted_count", 0))
        except Exception:
            logger.exception("GitWatcher: ошибка сбора снапшота для %s", repo_path)

    def _collect_snapshot(self, repo_path: Path) -> dict:
        """Синхронный сбор git-данных (запускается в thread pool)."""
        import subprocess

        def git(cmd: list[str]) -> str:
            try:
                result = subprocess.run(
                    ["git", "-C", str(repo_path)] + cmd,
                    capture_output=True, text=True, timeout=10
                )
                return result.stdout.strip()
            except Exception:
                return ""

        branch = git(["branch", "--show-current"]) or "HEAD detached"
        uncommitted = git(["status", "--porcelain"])
        uncommitted_count = len([line for line in uncommitted.splitlines() if line.strip()])

        last_commit_raw = git(["log", "-1", "--format=%H|%s|%ai"])
        last_commit = {}
        if last_commit_raw:
            parts = last_commit_raw.split("|", 2)
            if len(parts) == 3:
                last_commit = {"hash": parts[0][:8], "message": parts[1], "time": parts[2]}

        recent_log = git(["log", "--oneline", "-10"])

        unmerged = git(["branch", "--no-merged", "HEAD"])
        unmerged_branches = [b.strip() for b in unmerged.splitlines() if b.strip()]

        # TODOs и FIXMEs в коде
        todos_raw = git(["grep", "-n", "-E", "TODO|FIXME", "--", "*.py", "*.go", "*.ts", "*.js"])
        todos = todos_raw.splitlines()[:15] if todos_raw else []

        return {
            "name": repo_path.name,
            "path": str(repo_path),
            "branch": branch,
            "uncommitted_count": uncommitted_count,
            "last_commit": last_commit,
            "recent_log": recent_log,
            "unmerged_branches": unmerged_branches,
            "todos": todos,
        }

    async def get_all_snapshots(self) -> list[dict]:
        """Получить снапшоты всех репозиториев прямо сейчас (для контекста LLM)."""
        snapshots = []
        for repo in self._repos:
            try:
                snapshot = await asyncio.to_thread(self._collect_snapshot, repo)
                snapshots.append(snapshot)
            except Exception:
                logger.exception("GitWatcher: не удалось собрать снапшот %s", repo)
        return snapshots
