"""
errors/static_analyzer.py — запускает ruff и mypy на изменённых файлах.

Принцип:
- Слушает fs.batch от FSWatcher
- Debounce 30 секунд после последнего изменения Python-файла
- Запускает ruff (быстро) + mypy (медленнее) только на изменённых .py файлах
- Не запускается если пользователь активно работает (process.snapshot.is_working)
- Эмитит error.static { file, errors: [{ line, code, message, severity }] }
"""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)


class StaticAnalyzer:
    """
    Слушает fs.batch → debounce → ruff + mypy → error.static.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        err_cfg = config.get("errors", {})
        self._enabled:       bool = err_cfg.get("static_analysis", True)
        self._debounce_sec:  int  = err_cfg.get("debounce_sec", 30)

        self.bus = bus
        self._pending_files: set[str] = set()
        self._debounce_task: Optional[asyncio.Task] = None
        self._is_working: bool = False  # флаг из process.snapshot

    def subscribe(self) -> None:
        """Подписаться на события шины."""
        if not self._enabled:
            logger.debug("StaticAnalyzer: отключён в конфиге")
            return
        self.bus.on("fs.batch",        self._on_fs_batch)
        self.bus.on("process.snapshot", self._on_process_snapshot)
        logger.debug("StaticAnalyzer: подписки установлены")

    # ─── Обработчики событий ─────────────────────────────────────────────────

    async def _on_fs_batch(self, data: dict) -> None:
        """Получить список изменённых файлов и запланировать анализ."""
        if not data:
            return

        py_files = [
            ev["path"]
            for ev in data.get("events", [])
            if ev.get("ext") == ".py" and Path(ev["path"]).exists()
        ]

        if not py_files:
            return

        self._pending_files.update(py_files)

        # Сброс debounce-таймера
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        self._debounce_task = asyncio.create_task(
            self._debounced_analyze(), name="static_analyzer_debounce"
        )

    async def _on_process_snapshot(self, data: dict) -> None:
        """Обновить флаг активной работы."""
        if data:
            self._is_working = bool(data.get("is_working", False))

    # ─── Анализ ──────────────────────────────────────────────────────────────

    async def _debounced_analyze(self) -> None:
        """Ждём debounce_sec секунд, потом запускаем анализ."""
        try:
            await asyncio.sleep(self._debounce_sec)

            if self._is_working:
                logger.debug("StaticAnalyzer: пользователь работает — откладываем")
                # Перепланируем через ещё debounce_sec
                self._debounce_task = asyncio.create_task(
                    self._debounced_analyze(), name="static_analyzer_debounce"
                )
                return

            files = list(self._pending_files)
            self._pending_files.clear()

            if files:
                await self._analyze(files)

        except asyncio.CancelledError:
            pass  # Новое событие — debounce сброшен, задача отменена

    async def _analyze(self, files: list[str]) -> None:
        """Запустить ruff и mypy на файлах."""
        logger.debug("StaticAnalyzer: анализируем %d файлов", len(files))

        # Запускаем параллельно
        results = await asyncio.gather(
            self._run_ruff(files),
            self._run_mypy(files),
            return_exceptions=True,
        )

        # Объединяем ошибки по файлам
        errors_by_file: dict[str, list[dict]] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.debug("StaticAnalyzer: ошибка анализатора — %s", result)
                continue
            if isinstance(result, dict):
                for file_path, errs in result.items():
                    errors_by_file.setdefault(file_path, []).extend(errs)

        # Эмитим по файлу
        for file_path, errs in errors_by_file.items():
            if errs:
                await self.bus.emit("error.static", {
                    "file":   file_path,
                    "errors": errs,
                })
                logger.debug("StaticAnalyzer: %s → %d ошибок", file_path, len(errs))

    # ─── ruff ────────────────────────────────────────────────────────────────

    async def _run_ruff(self, files: list[str]) -> dict[str, list[dict]]:
        """
        ruff check {files} --output-format json
        Возвращает {file_path: [errors]}
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff", "check", "--output-format", "json", *files,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if not stdout.strip():
                return {}

            data = json.loads(stdout.decode("utf-8", errors="replace"))
            result: dict[str, list[dict]] = {}

            for item in data:
                fp = item.get("filename", "")
                err = {
                    "line":     item.get("location", {}).get("row"),
                    "code":     item.get("code", "ruff"),
                    "message":  item.get("message", ""),
                    "severity": "error" if item.get("code", "").startswith("E") else "warning",
                    "tool":     "ruff",
                }
                result.setdefault(fp, []).append(err)

            return result

        except FileNotFoundError:
            logger.debug("StaticAnalyzer: ruff не установлен — pip install ruff")
            return {}
        except asyncio.TimeoutError:
            logger.warning("StaticAnalyzer: ruff timeout")
            return {}
        except (json.JSONDecodeError, Exception) as e:
            logger.debug("StaticAnalyzer: ruff ошибка — %s", e)
            return {}

    # ─── mypy ────────────────────────────────────────────────────────────────

    async def _run_mypy(self, files: list[str]) -> dict[str, list[dict]]:
        """
        mypy {files} --no-error-summary
        Парсим текстовый вывод (mypy --output json нестабилен).
        Возвращает {file_path: [errors]}
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "mypy", "--no-error-summary", "--ignore-missing-imports", *files,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            if not stdout.strip():
                return {}

            # Формат: /path/to/file.py:10: error: message  [code]
            pattern = re.compile(
                r'^(.+?):(\d+):\s+(error|warning|note):\s+(.+?)(?:\s+\[([^\]]+)\])?$'
            )
            result: dict[str, list[dict]] = {}

            for line in stdout.decode("utf-8", errors="replace").splitlines():
                m = pattern.match(line.strip())
                if m:
                    fp       = m.group(1)
                    lineno   = int(m.group(2))
                    severity = m.group(3)
                    message  = m.group(4).strip()
                    code     = m.group(5) or "mypy"

                    if severity == "note":
                        continue  # notes не ошибки

                    err = {
                        "line":     lineno,
                        "code":     code,
                        "message":  message,
                        "severity": severity,
                        "tool":     "mypy",
                    }
                    result.setdefault(fp, []).append(err)

            return result

        except FileNotFoundError:
            logger.debug("StaticAnalyzer: mypy не установлен — pip install mypy")
            return {}
        except asyncio.TimeoutError:
            logger.warning("StaticAnalyzer: mypy timeout")
            return {}
        except Exception as e:
            logger.debug("StaticAnalyzer: mypy ошибка — %s", e)
            return {}
