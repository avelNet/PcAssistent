"""
collectors/clipboard_watcher.py — история буфера обмена через xclip.

Polling каждые 3 секунды (настраивается).
Данные только в памяти — не в SQLite (§3.7).

Фильтры:
- Минимум 20 символов
- Дедупликация по MD5
- Автоматически игнорирует секреты: password, secret, token, api_key,
  BEGIN RSA, BEGIN EC, eyJ (JWT)
"""

import asyncio
import hashlib
import logging
import re
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

# Паттерны которые указывают на секреты — не сохраняем
_SECRET_PATTERNS = re.compile(
    r'(password|passwd|secret|api[_\-]?key|access[_\-]?token|'
    r'private[_\-]?key|BEGIN\s+(RSA|EC|DSA|OPENSSH)|eyJ)',
    re.IGNORECASE,
)


def _is_secret(text: str) -> bool:
    return bool(_SECRET_PATTERNS.search(text))


class ClipboardWatcher:
    """
    Опрашивает буфер обмена через xclip каждые interval_sec секунд.
    Хранит последние max_history записей в памяти.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        clip_cfg = config.get("collectors", {}).get("clipboard", {})
        self._interval:   int = clip_cfg.get("interval_sec", 3)
        self._min_length: int = clip_cfg.get("min_length", 20)
        self._max_history: int = 50

        self.bus = bus
        self._history: deque[dict] = deque(maxlen=self._max_history)
        self._seen_hashes: set[str] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop(), name="clipboard_watcher")
        logger.info("ClipboardWatcher: запущен [interval=%ds]", self._interval)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        logger.info("ClipboardWatcher: остановлен")

    def get_history(self, limit: int = 10) -> list[dict]:
        """Вернуть последние N записей (для context_builder)."""
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
        """Читает содержимое буфера через xclip."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard", "-o",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            if proc.returncode == 0 and stdout:
                return stdout.decode("utf-8", errors="replace")
        except FileNotFoundError:
            # xclip не установлен — тихо отключаемся
            logger.debug("ClipboardWatcher: xclip не найден — sudo apt install xclip")
            self.stop()
        except asyncio.TimeoutError:
            logger.debug("ClipboardWatcher: xclip timeout")
        except Exception as e:
            logger.debug("ClipboardWatcher: ошибка xclip — %s", e)
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
