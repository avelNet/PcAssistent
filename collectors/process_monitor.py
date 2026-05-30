"""
process_monitor.py — мониторинг состояния пользователя.

Определяет:
  - заблокирован ли экран (loginctl LockedHint)
  - idle ли пользователь (loginctl IdleHint)
  - какие приложения активны (psutil)
  - начало/конец рабочей сессии

Работает на Wayland через loginctl — без xdotool/xprintidle.

Эмитит события:
  process.snapshot  — каждые interval_sec
  session.ended     — когда рабочая сессия закончилась (> work_session_min)
  user.returned     — когда вернулся после блокировки/idle (> idle_trigger_min)
"""

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field

import psutil

logger = logging.getLogger(__name__)

# Классификация процессов
_WORK_PROCS = {
    "pycharm", "idea", "webstorm", "goland", "clion", "rider",
    "code", "nvim", "vim", "emacs", "sublime_text",
    "python", "node", "cargo", "go", "java", "gradle",
    "terminal", "konsole", "gnome-terminal", "alacritty", "kitty", "wezterm",
    "bash", "zsh", "fish",
}
_LEISURE_PROCS = {
    "steam", "vlc", "mpv", "obs", "discord",
    "telegram-desktop", "spotify",
}


@dataclass
class ProcessSnapshot:
    is_working: bool = False
    is_leisure: bool = False
    is_locked: bool = False
    is_idle: bool = False
    idle_minutes: int = 0
    top_processes: list[str] = field(default_factory=list)


class ProcessMonitor:
    def __init__(self, config: dict, bus):
        cfg = config.get("collectors", {}).get("process", {})
        self.interval_sec: int = cfg.get("interval_sec", 60)

        trig = config.get("trigger", {})
        self.work_session_min: int = trig.get("work_session_min", 30)
        self.idle_trigger_min: int = trig.get("idle_trigger_hours", 2) * 60

        self.bus = bus
        self._session_id: str | None = None

        # Состояние сессии
        self._session_start_ts: float | None = None
        self._locked_at_ts: float | None = None
        self._idle_at_ts: float | None = None
        self._was_working: bool = False
        self._was_locked: bool = False
        self._last_tick_ts: float = time.time()  # для детекта suspend/resume

    async def start(self) -> None:
        self._session_id = await asyncio.to_thread(self._find_graphical_session)
        logger.info(
            "ProcessMonitor: запущен [session=%s, interval=%ds, "
            "work_min=%d, idle_trigger_min=%d]",
            self._session_id, self.interval_sec,
            self.work_session_min, self.idle_trigger_min
        )
        asyncio.create_task(self._loop(), name="process_monitor")

    async def _loop(self) -> None:
        while True:
            try:
                snapshot = await asyncio.to_thread(self._collect)
                await self._handle_snapshot(snapshot)
                await self.bus.emit("process.snapshot", {
                    "is_working": snapshot.is_working,
                    "is_leisure": snapshot.is_leisure,
                    "is_locked": snapshot.is_locked,
                    "idle_minutes": snapshot.idle_minutes,
                    "top_processes": snapshot.top_processes,
                })
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ProcessMonitor: ошибка в цикле")
            await asyncio.sleep(self.interval_sec)

    # ─── Сбор снапшота ───────────────────────────────────────────────────────

    def _collect(self) -> ProcessSnapshot:
        snap = ProcessSnapshot()

        # Один вызов loginctl — оба свойства сразу (меньше subprocess-флешей в доке)
        loginctl = self._loginctl_show()
        snap.is_locked = loginctl.get("LockedHint") == "yes"
        snap.is_idle   = loginctl.get("IdleHint")   == "yes"

        # Список активных процессов
        active_names: set[str] = set()
        try:
            for proc in psutil.process_iter(["name", "status"]):
                try:
                    name = (proc.info["name"] or "").lower()
                    if proc.info["status"] == psutil.STATUS_RUNNING:
                        active_names.add(name)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

        # Классификация
        snap.is_working = bool(active_names & _WORK_PROCS)
        snap.is_leisure = bool(active_names & _LEISURE_PROCS)

        # Топ процессов для контекста LLM
        snap.top_processes = sorted(
            active_names & (_WORK_PROCS | _LEISURE_PROCS)
        )[:10]

        return snap

    def _find_graphical_session(self) -> str | None:
        """Найти ID graphical сессии (seat0) через loginctl."""
        try:
            out = subprocess.check_output(
                ["loginctl", "list-sessions", "--no-legend"],
                text=True, timeout=5
            )
            for line in out.splitlines():
                parts = line.split()
                # Ищем строку с seat0 (графическая сессия)
                if len(parts) >= 4 and "seat0" in parts:
                    return parts[0]
            # Фолбэк — первая сессия
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    return parts[0]
        except Exception as e:
            logger.debug("ProcessMonitor: не удалось найти сессию: %s", e)
        return None

    def _loginctl_show(self) -> dict[str, str]:
        """
        Один вызов loginctl show-session → словарь всех свойств.
        При ошибке переоткрывает session_id (актуально после suspend/resume).
        """
        if not self._session_id:
            self._session_id = self._find_graphical_session()
        if not self._session_id:
            return {}
        try:
            out = subprocess.check_output(
                ["loginctl", "show-session", self._session_id],
                text=True, timeout=3,
                stderr=subprocess.DEVNULL,
            )
            props: dict[str, str] = {}
            for line in out.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
            return props
        except Exception:
            # Сессия недоступна (suspend/resume) — сбрасываем, найдём заново
            logger.debug("ProcessMonitor: сессия %s недоступна, переоткрываю", self._session_id)
            self._session_id = None
            return {}

    def _check_locked(self) -> bool:
        return self._loginctl_show().get("LockedHint") == "yes"

    def _check_idle(self) -> bool:
        return self._loginctl_show().get("IdleHint") == "yes"

    # ─── Логика сессий и триггеров ───────────────────────────────────────────

    async def _handle_snapshot(self, snap: ProcessSnapshot) -> None:
        now = time.time()

        # ── Детект suspend/resume ──────────────────────────────────────────
        # Если между тиками прошло намного больше interval_sec — машина спала.
        # Эмитируем user.returned если пробуждение после >5 мин сна.
        gap = now - self._last_tick_ts
        if gap > self.interval_sec * 3:  # проснулись после длинной паузы
            sleep_min = gap / 60
            logger.info("ProcessMonitor: пробуждение после %.0f мин сна", sleep_min)
            if sleep_min >= 5:
                await self.bus.emit("user.returned", {
                    "idle_was_min": round(sleep_min),
                    "reason": "resume",
                })
            # Сбрасываем состояния — они устарели за время сна
            self._locked_at_ts = None
            self._was_locked   = False
            self._session_start_ts = None
            self._was_working  = False
        self._last_tick_ts = now

        # ── Блокировка экрана ──────────────────────────────────────────────
        if snap.is_locked and not self._was_locked:
            # Экран только что заблокировали
            self._locked_at_ts = now
            self._was_locked = True
            logger.debug("ProcessMonitor: экран заблокирован")

            # Если была активная рабочая сессия — завершаем её
            if self._session_start_ts:
                duration_min = (now - self._session_start_ts) / 60
                if duration_min >= self.work_session_min:
                    logger.info(
                        "ProcessMonitor: сессия завершена при блокировке (%.0f мин)", duration_min
                    )
                    await self.bus.emit("session.ended", {"duration_min": duration_min})
                self._session_start_ts = None

        elif not snap.is_locked and self._was_locked:
            # Экран разблокирован — пользователь вернулся
            self._was_locked = False
            if self._locked_at_ts:
                away_min = (now - self._locked_at_ts) / 60
                self._locked_at_ts = None
                logger.info("ProcessMonitor: экран разблокирован, отсутствовал %.0f мин", away_min)

                if away_min >= 5:  # меньше 5 минут — не считаем
                    await self.bus.emit("user.returned", {
                        "idle_was_min": round(away_min),
                        "reason": "screen_unlock",
                    })

        # ── Рабочая сессия (только когда экран не заблокирован) ───────────
        if snap.is_locked:
            return

        if snap.is_working and not self._was_working:
            # Началась рабочая сессия
            self._session_start_ts = now
            self._was_working = True
            logger.debug("ProcessMonitor: рабочая сессия началась")

        elif not snap.is_working and self._was_working:
            # Рабочая сессия закончилась
            self._was_working = False
            if self._session_start_ts:
                duration_min = (now - self._session_start_ts) / 60
                self._session_start_ts = None
                if duration_min >= self.work_session_min:
                    logger.info(
                        "ProcessMonitor: рабочая сессия завершена (%.0f мин)", duration_min
                    )
                    await self.bus.emit("session.ended", {"duration_min": duration_min})

        # ── Idle без блокировки (долго не трогал мышь/клавиатуру) ────────
        if snap.is_idle and not self._was_locked:
            if not self._idle_at_ts:
                self._idle_at_ts = now
        else:
            if self._idle_at_ts:
                idle_min = (now - self._idle_at_ts) / 60
                self._idle_at_ts = None
                if idle_min >= self.idle_trigger_min:
                    logger.info("ProcessMonitor: вернулся после idle %.0f мин", idle_min)
                    await self.bus.emit("user.returned", {
                        "idle_was_min": round(idle_min),
                        "reason": "idle",
                    })
