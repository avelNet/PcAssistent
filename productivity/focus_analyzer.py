"""
productivity/focus_analyzer.py — определяет активное окно через xdotool.

Опрашивает каждые 30 секунд.
Классификация:
  deep_work   — PyCharm, IDEA, Terminal, nvim, vim, Emacs, VSCode
  browser     — Firefox, Chromium, Chrome
  distraction — YouTube (по заголовку), Telegram, Discord, VK
  leisure     — Steam, vlc, mpv, obs
  other       — всё остальное

Эмитит: focus.changed { from_app, to_app, category, duration_sec }
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from core.event_bus import EventBus

logger = logging.getLogger(__name__)

# WM_CLASS → категория
_CLASS_MAP: dict[str, str] = {
    # deep_work
    "jetbrains-pycharm":  "deep_work",
    "jetbrains-idea":     "deep_work",
    "jetbrains-goland":   "deep_work",
    "jetbrains-webstorm": "deep_work",
    "jetbrains-clion":    "deep_work",
    "code":               "deep_work",   # VSCode
    "code-oss":           "deep_work",
    "nvim":               "deep_work",
    "vim":                "deep_work",
    "emacs":              "deep_work",
    "gnome-terminal":     "deep_work",
    "konsole":            "deep_work",
    "kitty":              "deep_work",
    "alacritty":          "deep_work",
    "tilix":              "deep_work",
    # browser
    "firefox":            "browser",
    "firefox-esr":        "browser",
    "chromium-browser":   "browser",
    "chromium":           "browser",
    "google-chrome":      "browser",
    # distraction (мессенджеры)
    "telegram-desktop":   "distraction",
    "discord":            "distraction",
    "slack":              "distraction",
    # leisure
    "steam":              "leisure",
    "vlc":                "leisure",
    "mpv":                "leisure",
    "obs":                "leisure",
}

# Заголовки которые превращают browser → distraction
_DISTRACTION_TITLES = ("youtube", "вконтакте", "vk.com", "twitch", "netflix")


def _classify(wm_class: str, title: str) -> str:
    """Определить категорию по WM_CLASS и заголовку окна."""
    cls = wm_class.lower().strip()
    category = _CLASS_MAP.get(cls, "other")

    if category == "browser":
        t = title.lower()
        if any(d in t for d in _DISTRACTION_TITLES):
            category = "distraction"

    return category


class FocusAnalyzer:
    """
    Опрашивает активное окно через xdotool каждые poll_sec секунд.
    При смене окна эмитит focus.changed.
    """

    def __init__(self, config: dict, bus: "EventBus"):
        prod_cfg = config.get("productivity", {})
        self._poll_sec: int = prod_cfg.get("focus_poll_sec", 30)
        self.bus = bus
        self._task: Optional[asyncio.Task] = None

        self._current_class:    str   = ""
        self._current_title:    str   = ""
        self._current_category: str   = "other"
        self._window_since:     float = time.time()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop(), name="focus_analyzer")
        logger.info("FocusAnalyzer: запущен [interval=%ds]", self._poll_sec)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        logger.info("FocusAnalyzer: остановлен")

    async def get_current(self) -> dict:
        """Текущее состояние фокуса."""
        return {
            "app":      self._current_class,
            "title":    self._current_title,
            "category": self._current_category,
        }

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_sec)
                await self._check_focus()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("FocusAnalyzer: ошибка poll", exc_info=True)

    async def _check_focus(self) -> None:
        """Прочитать активное окно и при изменении эмитить событие."""
        wm_class, title = await self._get_active_window()
        if not wm_class:
            return

        category = _classify(wm_class, title)

        if wm_class == self._current_class and category == self._current_category:
            return

        now = time.time()
        duration = now - self._window_since

        # Эмитим только если предыдущий фокус был хоть сколько-нибудь
        if self._current_class and duration >= 5:
            await self.bus.emit("focus.changed", {
                "from_app":    self._current_class,
                "to_app":      wm_class,
                "from_cat":    self._current_category,
                "to_cat":      category,
                "title":       title,
                "duration_sec": int(duration),
            })
            logger.debug(
                "FocusAnalyzer: %s(%s) → %s(%s) [%ds]",
                self._current_class, self._current_category,
                wm_class, category, int(duration),
            )

        self._current_class    = wm_class
        self._current_title    = title
        self._current_category = category
        self._window_since     = now

    async def _get_active_window(self) -> tuple[str, str]:
        """Вернуть (wm_class, title) активного окна через xdotool."""
        try:
            # Получаем ID окна
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "getactivewindow",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
            if proc.returncode != 0 or not stdout.strip():
                return "", ""
            wid = stdout.strip().decode()

            # WM_CLASS и заголовок параллельно
            class_proc, title_proc = await asyncio.gather(
                asyncio.create_subprocess_exec(
                    "xdotool", "getwindowclassname", wid,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                asyncio.create_subprocess_exec(
                    "xdotool", "getwindowname", wid,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
            )
            class_out, _ = await asyncio.wait_for(class_proc.communicate(), timeout=2)
            title_out, _ = await asyncio.wait_for(title_proc.communicate(), timeout=2)

            wm_class = class_out.strip().decode("utf-8", errors="replace")
            title    = title_out.strip().decode("utf-8", errors="replace")
            return wm_class, title

        except FileNotFoundError:
            logger.debug("FocusAnalyzer: xdotool не установлен — sudo apt install xdotool")
            self.stop()
            return "", ""
        except asyncio.TimeoutError:
            return "", ""
        except Exception as e:
            logger.debug("FocusAnalyzer: ошибка — %s", e)
            return "", ""
