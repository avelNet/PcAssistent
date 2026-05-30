"""
collectors/clipboard_watcher.py — история буфера обмена.

Polling каждые 3 секунды (настраивается).
Данные только в памяти — не в SQLite (§3.7).

Поддерживаемые инструменты (автовыбор):
- wl-paste  — Wayland (приоритет если WAYLAND_DISPLAY)
- xclip     — X11
- xsel      — X11 fallback

Фильтры:
- Минимум 20 символов
- Дедупликация по MD5
- Автоматически игнорирует секреты: password, secret, token, api_key,
  BEGIN RSA, BEGIN EC, eyJ (JWT)
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = re.compile(
    r'(password|passwd|secret|api[_\-]?key|access[_\-]?token|'
    r'private[_\-]?key|BEGIN\s+(RSA|EC|DSA|OPENSSH)|eyJ)',
    re.IGNORECASE,
)

# Команды чтения буфера в порядке приоритета
_CLIPBOARD_CMDS = [
    (["wl-paste", "--no-newline"],          "Wayland"),
    (["xclip", "-selection", "clipboard", "-o"], "X11/xclip"),
    (["xsel", "--clipboard", "--output"],   "X11/xsel"),
]


def _is_secret(text: str) -> bool:
    return bool(_SECRET_PATTERNS.search(text))


class ClipboardWatcher:
    """
    Опрашивает буфер обмена каждые interval_sec секунд.
    Автоматически выбирает wl-paste (Wayland) или xclip/xsel (X11).
    """

    def __init__(self, config: dict, bus: "EventBus"):
        clip_cfg = config.get("collectors", {}).get("clipboard", {})
        self._interval:    int = clip_cfg.get("interval_sec", 3)
        self._min_length:  int = clip_cfg.get("min_length", 20)
        self._max_history: int = 50

        self.bus = bus
        self._history:     deque[dict] = deque(maxlen=self._max_history)
        self._seen_hashes: set[str]   = set()
        self._task:        asyncio.Task | None = None
        self._cmd:         list[str] | None    = None  # рабочая команда (для X11 polling)
        self._wl_proc:     asyncio.subprocess.Process | None = None  # для Wayland watch

    async def start(self) -> None:
        # На Wayland используем wl-paste --watch (event-driven, не polling)
        # — иначе каждый запуск wl-paste создаёт временное окно которое
        # дёргает GNOME Shell (видно как мерцание иконок в доке)
        if os.environ.get("WAYLAND_DISPLAY"):
            self._task = asyncio.create_task(
                self._watch_wayland(), name="clipboard_watcher_wl"
            )
            logger.info("ClipboardWatcher: запущен [Wayland event-driven]")
        else:
            self._task = asyncio.create_task(
                self._poll_loop(), name="clipboard_watcher_poll"
            )
            logger.info("ClipboardWatcher: запущен [X11 polling, interval=%ds]", self._interval)

    def stop(self) -> None:
        if self._wl_proc and self._wl_proc.returncode is None:
            try:
                self._wl_proc.terminate()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        logger.info("ClipboardWatcher: остановлен")

    async def _watch_wayland(self) -> None:
        """
        wl-paste --watch cat — долгоживущий процесс с авто-перезапуском.
        После suspend/resume wl-paste может умереть — перезапускаем через 5 сек.
        """
        while True:
            try:
                self._wl_proc = await asyncio.create_subprocess_exec(
                    "wl-paste", "--watch", "cat",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except FileNotFoundError:
                logger.warning(
                    "ClipboardWatcher: wl-paste не найден — "
                    "sudo apt install wl-clipboard"
                )
                return  # нет смысла retry если пакет не установлен

            logger.debug("ClipboardWatcher: wl-paste запущен (pid=%s)", self._wl_proc.pid)
            buffer = bytearray()

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            self._wl_proc.stdout.read(65536),
                            timeout=0.5,
                        )
                    except asyncio.TimeoutError:
                        if buffer:
                            text = buffer.decode("utf-8", errors="replace")
                            self._process(text)
                            buffer.clear()
                        continue

                    if not chunk:  # EOF — процесс умер
                        logger.debug("ClipboardWatcher: wl-paste завершился, перезапуск через 5 сек")
                        break

                    buffer.extend(chunk)
                    if len(buffer) > 1_000_000:
                        buffer.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("ClipboardWatcher: ошибка чтения wl-paste", exc_info=True)

            await asyncio.sleep(5)  # пауза перед перезапуском

    def get_history(self, limit: int = 10) -> list[dict]:
        items = list(self._history)
        return items[-limit:]

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                text = await self._read_clipboard()
                if text:
                    self._process(text)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("ClipboardWatcher: ошибка опроса", exc_info=True)

    async def _read_clipboard(self) -> str | None:
        """
        Читает буфер обмена. Пробует команды в порядке приоритета,
        запоминает первую рабочую и использует её в дальнейшем.
        """
        is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        candidates = sorted(
            _CLIPBOARD_CMDS,
            key=lambda x: (0 if "wl-paste" in x[0] else 1) if is_wayland
                     else (0 if "wl-paste" not in x[0] else 1),
        )

        if self._cmd:
            candidates = [next((c for c in candidates if c[0] == self._cmd), candidates[0])]

        for cmd, label in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
                if proc.returncode == 0 and stdout:
                    if self._cmd is None:
                        self._cmd = cmd
                        logger.info("ClipboardWatcher: использую %s", label)
                    return stdout.decode("utf-8", errors="replace")
                return None  # пустой буфер — норма
            except FileNotFoundError:
                logger.debug("ClipboardWatcher: %s не найден", label)
                continue
            except asyncio.TimeoutError:
                logger.debug("ClipboardWatcher: timeout (%s)", label)
                continue
            except Exception as e:
                logger.debug("ClipboardWatcher: ошибка %s — %s", label, e)
                continue

        # Ни один инструмент не работает — логируем один раз и ждём
        if self._cmd is None:
            logger.warning(
                "ClipboardWatcher: нет инструмента для чтения буфера. "
                "Установи: sudo apt install wl-clipboard  (Wayland) "
                "или  sudo apt install xclip  (X11)"
            )
            self._cmd = []  # пустой список = "уже предупредили"
        return None

    def _process(self, text: str) -> None:
        """Фильтрует и сохраняет запись в историю."""
        text = text.strip()

        if len(text) < self._min_length:
            return

        if _is_secret(text):
            logger.debug("ClipboardWatcher: пропущен секрет (%d символов)", len(text))
            return

        md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
        if md5 in self._seen_hashes:
            return

        self._seen_hashes.add(md5)
        # Ограничиваем размер seen_hashes — убираем старые при росте
        if len(self._seen_hashes) > 500:
            self._seen_hashes = set(list(self._seen_hashes)[-200:])

        entry = {
            "text": text[:2000],   # не храним огромные куски
            "ts":   time.time(),
            "len":  len(text),
        }
        self._history.append(entry)
        logger.debug("ClipboardWatcher: новая запись %d символов", len(text))
