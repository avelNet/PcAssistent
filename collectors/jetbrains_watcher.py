"""
collectors/jetbrains_watcher.py — читает XML-файлы JetBrains IDE.

Поддерживаемые IDE: IntelliJIdea, PyCharm, GoLand, WebStorm, CLion
(glob по ~/.config/JetBrains/*)

Что читает:
- recentProjects.xml  — последние проекты + время открытия
- workspace.xml       — открытые вкладки, breakpoints
"""

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

# Паттерн имён папок JetBrains в ~/.config/JetBrains/
_IDE_GLOB = "*/options"


class JetBrainsWatcher:
    """
    Читает XML-файлы JetBrains IDE по требованию (не inotify — файлы меняются редко).
    Опрашивает каждые 120 секунд и при старте.
    Эмитит jetbrains.changed если данные изменились.
    """

    POLL_INTERVAL = 30  # секунд — нужно быстро реагировать на смену активного проекта

    def __init__(self, config: dict, bus: "EventBus"):
        cfg = config.get("collectors", {}).get("jetbrains", {})
        self._config_dir = Path(
            cfg.get("config_dir", "~/.config/JetBrains")
        ).expanduser()
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._last_snapshot: dict | None = None

    async def start(self) -> None:
        if not self._config_dir.exists():
            logger.debug("JetBrainsWatcher: %s не найден — пропускаем", self._config_dir)
            return
        self._task = asyncio.create_task(self._poll_loop(), name="jetbrains_watcher")
        logger.info("JetBrainsWatcher: запущен [%s]", self._config_dir)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        logger.info("JetBrainsWatcher: остановлен")

    async def get_snapshot(self) -> dict:
        """Вернуть актуальный снапшот (для context_builder)."""
        return await asyncio.to_thread(self._read_all)

    # ─── Polling ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self._read_all)
                if snapshot and snapshot != self._last_snapshot:
                    self._last_snapshot = snapshot
                    await self.bus.emit("jetbrains.changed", snapshot)
                    logger.debug(
                        "JetBrainsWatcher: изменение — проекты=%d, breakpoints=%d",
                        len(snapshot.get("projects", [])),
                        len(snapshot.get("breakpoints", [])),
                    )
                await asyncio.sleep(self.POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("JetBrainsWatcher: ошибка poll")
                await asyncio.sleep(60)

    # ─── Парсинг XML ─────────────────────────────────────────────────────────

    def _detect_active_project(self, projects: list[dict]) -> str | None:
        """
        Определить активный сейчас проект по самому свежему mtime файлов в .idea/.
        Берём максимум среди всех файлов директории — workspace.xml сохраняется
        лениво, но другие файлы (modules.xml, индексы, caches) могут обновляться
        быстрее при работе с проектом.
        """
        best_name: str | None = None
        best_mtime: float = 0.0
        for p in projects:
            idea_dir = Path(p.get("path", "")) / ".idea"
            if not idea_dir.is_dir():
                continue
            try:
                # Максимальный mtime среди файлов в .idea/ (без рекурсии)
                m = max(
                    (f.stat().st_mtime for f in idea_dir.iterdir() if f.is_file()),
                    default=0.0,
                )
                # Также берём mtime самой директории (обновляется при создании файлов)
                m = max(m, idea_dir.stat().st_mtime)
            except OSError:
                continue
            if m > best_mtime:
                best_mtime = m
                best_name = p.get("name")
        return best_name

    def _read_all(self) -> dict:
        """Прочитать все IDE и вернуть объединённый снапшот."""
        projects: list[dict] = []
        open_files: list[str] = []
        breakpoints: list[dict] = []

        # Ищем все установленные JetBrains IDE
        ide_dirs = sorted(self._config_dir.glob("*"))
        for ide_dir in ide_dirs:
            if not ide_dir.is_dir():
                continue

            options_dir = ide_dir / "options"
            if not options_dir.exists():
                continue

            # recentProjects.xml
            recent_path = options_dir / "recentProjects.xml"
            if recent_path.exists():
                try:
                    proj_data = self._parse_recent_projects(recent_path)
                    projects.extend(proj_data)
                except Exception as e:
                    logger.debug("JetBrains: ошибка recentProjects %s: %s", recent_path, e)

            # workspace.xml для каждого открытого проекта
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

        return {
            "projects":   projects[:10],             # топ-10 последних
            "active_project": self._detect_active_project(projects),  # самый недавний по mtime
            "open_files": list(dict.fromkeys(open_files))[:20],  # дедупликация, топ-20
            "breakpoints": breakpoints[:30],
            "ts": time.time(),
        }

    def _parse_recent_projects(self, xml_path: Path) -> list[dict]:
        """
        Парсит recentProjects.xml.
        Возвращает: [{ path, name }]

        Поддерживает два формата JetBrains:
          Старый: RecentProjectsManager > option[recentPaths] > list > option[value=path]
          Новый:  RecentProjectsManager > option[additionalInfo] > map > entry[key=path]
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()
        results = []

        for comp in root.iter("component"):
            if comp.get("name") != "RecentProjectsManager":
                continue

            # Новый формат (PyCharm 2024+): additionalInfo > map > entry[key=path]
            for opt in comp.iter("option"):
                if opt.get("name") == "additionalInfo":
                    for entry in opt.iter("entry"):
                        raw_path = entry.get("key", "")
                        if raw_path:
                            clean = raw_path.replace("$USER_HOME$", str(Path.home()))
                            p = Path(clean)
                            if p.exists():
                                results.append({"path": str(p), "name": p.name})
                    break

            # Старый формат: recentPaths > list > option[value=path]
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
                                    results.append({"path": str(p), "name": p.name})
                        break

        return results

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

            # Открытые вкладки
            if name == "FileEditorManager":
                for leaf in comp.iter("leaf"):
                    for entry in leaf.iter("entry"):
                        f = entry.get("file", "")
                        if f:
                            # file:///.../src/main.py → /home/.../src/main.py
                            clean = re.sub(r'^file://', '', f)
                            open_files.append(clean)

            # Breakpoints
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
