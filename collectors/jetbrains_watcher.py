"""
collectors/jetbrains_watcher.py — читает XML-файлы JetBrains IDE.

Поддерживаемые IDE: IntelliJIdea, PyCharm, GoLand, WebStorm, CLion
(glob по ~/.config/JetBrains/*)

Что читает:
- recentProjects.xml  — последние проекты + opened/activationTimestamp
- workspace.xml       — открытые вкладки, breakpoints

Как определяем активный проект (надёжный метод):
  1. opened="true" + максимальный activationTimestamp  → активный сейчас
  2. lastOpenedProject → если нет opened=true (проект закрыт и открыт заново)
  Мы НЕ используем mtime файлов .idea/ — IDE обновляет их в фоне (индексы, кэши).

Обнаружение изменений (два уровня):
  1. watchdog inotify → мгновенная реакция когда PyCharm пишет recentProjects.xml
  2. stat()-polling каждые 5 сек → запасной канал если inotify пропустил
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


class JetBrainsWatcher:
    """
    Следит за recentProjects.xml через watchdog inotify (мгновенно)
    + stat()-polling каждые 5 сек (запасной канал).
    Полный XML-парсинг — только при изменении файла.
    Эмитит jetbrains.changed если активный проект сменился.
    """

    STAT_INTERVAL = 5    # секунд — частота stat()-polling
    FULL_POLL     = 120  # секунд — принудительный полный перечит

    def __init__(self, config: dict, bus: "EventBus"):
        cfg = config.get("collectors", {}).get("jetbrains", {})
        self._config_dir = Path(
            cfg.get("config_dir", "~/.config/JetBrains")
        ).expanduser()
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._last_snapshot: dict | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._xml_mtimes: dict[str, float] = {}
        self._last_full_poll: float = 0.0

        # asyncio.Event — watchdog сигнализирует из своего потока
        self._changed_event: asyncio.Event | None = None
        self._observer = None

    async def start(self) -> None:
        if not self._config_dir.exists():
            logger.debug("JetBrainsWatcher: %s не найден — пропускаем", self._config_dir)
            return
        self._loop = asyncio.get_running_loop()
        self._changed_event = asyncio.Event()
        self._start_inotify()
        self._task = asyncio.create_task(self._poll_loop(), name="jetbrains_watcher")
        logger.info("JetBrainsWatcher: запущен [%s]", self._config_dir)

    def _start_inotify(self) -> None:
        """Запустить watchdog observer на recentProjects.xml файлы."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watcher = self

            class _Handler(FileSystemEventHandler):
                def on_modified(self, event):
                    if "recentProjects.xml" in event.src_path:
                        loop = watcher._loop
                        ev   = watcher._changed_event
                        if loop and ev:
                            loop.call_soon_threadsafe(ev.set)

                def on_created(self, event):
                    self.on_modified(event)

            self._observer = Observer()
            # Наблюдаем за всеми options/ папками IDE
            watched = set()
            for xml in self._find_recent_project_xmls():
                options_dir = str(xml.parent)
                if options_dir not in watched:
                    self._observer.schedule(_Handler(), options_dir, recursive=False)
                    watched.add(options_dir)

            if watched:
                self._observer.start()
                logger.info("JetBrainsWatcher: inotify запущен (%d папок)", len(watched))
            else:
                logger.debug("JetBrainsWatcher: нет recentProjects.xml для inotify")
        except Exception as e:
            logger.warning("JetBrainsWatcher: inotify недоступен — %s", e)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception:
                pass
        logger.info("JetBrainsWatcher: остановлен")

    async def get_snapshot(self) -> dict:
        """Вернуть актуальный снапшот (для context_builder)."""
        return await asyncio.to_thread(self._read_all)

    # ─── Polling ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                # Ждём либо сигнала от inotify, либо таймаута polling
                if self._changed_event:
                    try:
                        await asyncio.wait_for(
                            self._changed_event.wait(),
                            timeout=self.STAT_INTERVAL,
                        )
                        self._changed_event.clear()
                        triggered_by = "inotify"
                    except asyncio.TimeoutError:
                        triggered_by = "poll"
                else:
                    await asyncio.sleep(self.STAT_INTERVAL)
                    triggered_by = "poll"

                # При poll — проверяем mtime; при inotify — всегда читаем
                force = (time.time() - self._last_full_poll) >= self.FULL_POLL
                if triggered_by == "inotify" or force or await asyncio.to_thread(self._xml_files_changed):
                    self._last_full_poll = time.time()
                    snapshot = await asyncio.to_thread(self._read_all)
                    if snapshot and snapshot != self._last_snapshot:
                        prev_active = (self._last_snapshot or {}).get("active_project")
                        new_active  = snapshot.get("active_project")
                        self._last_snapshot = snapshot
                        await self.bus.emit("jetbrains.changed", snapshot)
                        if new_active != prev_active:
                            logger.info(
                                "JetBrainsWatcher: активный проект %s → %s [via %s]",
                                prev_active or "нет", new_active or "нет", triggered_by,
                            )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("JetBrainsWatcher: ошибка poll")
                await asyncio.sleep(30)

    def _xml_files_changed(self) -> bool:
        """
        Быстрая проверка: изменился ли хоть один recentProjects.xml?
        Только stat() — без парсинга.
        """
        changed = False
        for xml_path in self._find_recent_project_xmls():
            try:
                mtime = os.stat(xml_path).st_mtime
            except OSError:
                continue
            path_str = str(xml_path)
            if self._xml_mtimes.get(path_str) != mtime:
                self._xml_mtimes[path_str] = mtime
                changed = True
        return changed

    def _find_recent_project_xmls(self) -> list[Path]:
        """Найти все recentProjects.xml во всех установленных IDE."""
        result = []
        try:
            for ide_dir in sorted(self._config_dir.iterdir()):
                if not ide_dir.is_dir():
                    continue
                xml = ide_dir / "options" / "recentProjects.xml"
                if xml.exists():
                    result.append(xml)
        except OSError:
            pass
        return result

    # ─── Парсинг XML ─────────────────────────────────────────────────────────

    def _detect_active_project(self, projects: list[dict], last_opened: str | None) -> str | None:
        """
        Определить активный проект:
          1. Среди opened=True → берём с максимальным activationTimestamp
          2. Если нет opened → используем lastOpenedProject
          3. Иначе → проект с максимальным activationTimestamp
        """
        opened = [p for p in projects if p.get("opened")]
        if opened:
            best = max(opened, key=lambda p: p.get("activation_ts", 0))
            return best.get("name")

        if last_opened:
            p = Path(last_opened)
            if p.exists():
                return p.name

        if projects:
            best = max(projects, key=lambda p: p.get("activation_ts", 0))
            return best.get("name")

        return None

    def _read_all(self) -> dict:
        """Прочитать все IDE и вернуть объединённый снапшот."""
        projects: list[dict] = []
        open_files: list[str] = []
        breakpoints: list[dict] = []
        last_opened: str | None = None

        for xml_path in self._find_recent_project_xmls():
            try:
                proj_data, lo = self._parse_recent_projects(xml_path)
                projects.extend(proj_data)
                if lo:
                    last_opened = lo
            except Exception as e:
                logger.debug("JetBrains: ошибка recentProjects %s: %s", xml_path, e)

        # workspace.xml для каждого проекта
        for proj in projects:
            proj_path = Path(proj.get("path", ""))
            ws_path = proj_path / ".idea" / "workspace.xml"
            if ws_path.exists():
                try:
                    files, bps = self._parse_workspace(ws_path)
                    open_files.extend(files)
                    breakpoints.extend(bps)
                except Exception as e:
                    logger.debug("JetBrains: ошибка workspace %s: %s", ws_path, e)

        active = self._detect_active_project(projects, last_opened)

        return {
            "projects":       projects[:10],
            "active_project": active,
            "open_files":     list(dict.fromkeys(open_files))[:20],
            "breakpoints":    breakpoints[:30],
            "ts":             time.time(),
        }

    def _parse_recent_projects(self, xml_path: Path) -> tuple[list[dict], str | None]:
        """
        Парсит recentProjects.xml.
        Возвращает: ([{ path, name, opened, activation_ts }], last_opened_path)

        Ключевые поля в XML (PyCharm 2024+):
          opened="true"                      — проект открыт сейчас
          option name="activationTimestamp"  — мс, когда был последний раз активирован
          option name="lastOpenedProject"    — абсолютный путь к последнему проекту
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()
        results: list[dict] = []
        last_opened: str | None = None

        for comp in root.iter("component"):
            if comp.get("name") != "RecentProjectsManager":
                continue

            # lastOpenedProject
            for opt in comp.iter("option"):
                if opt.get("name") == "lastOpenedProject":
                    raw = opt.get("value", "")
                    last_opened = raw.replace("$USER_HOME$", str(Path.home()))

            # additionalInfo (PyCharm 2024+)
            for opt in comp.iter("option"):
                if opt.get("name") != "additionalInfo":
                    continue
                for entry in opt.iter("entry"):
                    raw_path = entry.get("key", "")
                    if not raw_path:
                        continue
                    clean = raw_path.replace("$USER_HOME$", str(Path.home()))
                    p = Path(clean)
                    if not p.exists():
                        continue

                    meta = entry.find(".//RecentProjectMetaInfo")
                    opened = False
                    activation_ts = 0
                    if meta is not None:
                        opened = meta.get("opened", "false").lower() == "true"
                        for m_opt in meta.iter("option"):
                            if m_opt.get("name") == "activationTimestamp":
                                try:
                                    activation_ts = int(m_opt.get("value", 0))
                                except ValueError:
                                    pass

                    results.append({
                        "path":          str(p),
                        "name":          p.name,
                        "opened":        opened,
                        "activation_ts": activation_ts,
                    })
                break  # нашли additionalInfo — старый формат не нужен

            # Старый формат (fallback): recentPaths > list > option[value=path]
            if not results:
                for opt in comp.iter("option"):
                    if opt.get("name") == "recentPaths":
                        lst = opt.find("list")
                        if lst is not None:
                            for entry in lst:
                                raw_path = entry.get("value", "")
                                clean = raw_path.replace("$USER_HOME$", str(Path.home()))
                                p = Path(clean)
                                if p.exists():
                                    results.append({
                                        "path":          str(p),
                                        "name":          p.name,
                                        "opened":        False,
                                        "activation_ts": 0,
                                    })
                        break

        return results, last_opened

    def _parse_workspace(self, xml_path: Path) -> tuple[list[str], list[dict]]:
        """
        Парсит .idea/workspace.xml.
        Возвращает: (open_files, breakpoints)
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()

        open_files: list[str] = []
        breakpoints: list[dict] = []

        for comp in root.iter("component"):
            name = comp.get("name", "")

            if name == "FileEditorManager":
                for leaf in comp.iter("leaf"):
                    for entry in leaf.iter("entry"):
                        f = entry.get("file", "")
                        if f:
                            clean = re.sub(r'^file://', '', f)
                            open_files.append(clean)

            if name == "XBreakpointManager":
                for bp in comp.iter("breakpoint"):
                    file_url = bp.get("file-url", "")
                    line = bp.get("line", "")
                    bp_type = bp.get("type", "")
                    if file_url:
                        clean_file = re.sub(r'^file://', '', file_url)
                        breakpoints.append({
                            "file": clean_file,
                            "line": int(line) + 1 if line.isdigit() else None,
                            "type": bp_type,
                        })

        return open_files, breakpoints
