"""
notifier.py — desktop-уведомления через notify-send + открытие Obsidian.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_PRIORITY_ICON = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}
_MAX_TITLE_LEN = 42   # символов в одной строке уведомления


def _short(text: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """Обрезать текст до max_len символов."""
    text = text.strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


async def notify(
    title: str,
    body: str = "",
    urgency: str = "normal",
    timeout_ms: int = 8000,
    icon: str = "dialog-information",
) -> None:
    """Отправить desktop-уведомление. Не бросает исключений."""
    try:
        cmd = [
            "notify-send",
            "--urgency", urgency,
            "--expire-time", str(timeout_ms),
            "--icon", icon,
            title,
        ]
        if body:
            cmd.append(body)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass
    except FileNotFoundError:
        logger.debug("notifier: notify-send не установлен")
    except Exception as e:
        logger.debug("notifier: ошибка — %s", e)


async def _notify_send_with_action(
    title: str,
    body: str,
) -> tuple[bool, str | None]:
    """
    notify-send ≥0.8 с --action и --activation-token-fd.
    Ждёт клика пользователя (или истечения expire-time).
    Возвращает (clicked, activation_token).

    Полностью заменяет GLib.MainLoop-подход — тот создавал временную запись
    приложения в доке GNOME при каждом вызове (мерцание).
    """
    r_fd, w_fd = os.pipe()
    cmd = [
        "notify-send",
        "--app-name=PC Assistant",
        "--app-icon=appointment-new",
        "--expire-time=30000",
        f"--activation-token-fd={w_fd}",
        "--action=open:Открыть Obsidian",
        title,
    ]
    if body:
        cmd.append(body)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=(w_fd,),
        )
        os.close(w_fd)  # закрываем write-конец в родительском процессе

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                os.close(r_fd)
            except OSError:
                pass
            return False, None

        # Читаем activation token — после завершения notify-send write-конец закрыт,
        # поэтому read() вернёт сразу (EOF или данные)
        try:
            token_raw = os.read(r_fd, 512)
        except OSError:
            token_raw = b""
        finally:
            try:
                os.close(r_fd)
            except OSError:
                pass

        token = token_raw.decode().strip() or None
        action = stdout.decode().strip()
        return action == "open", token

    except FileNotFoundError:
        logger.debug("notifier: notify-send не найден")
        for fd in (r_fd, w_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        return False, None
    except Exception as e:
        logger.debug("notifier: notify-send+action ошибка — %s", e)
        for fd in (r_fd, w_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        return False, None


async def notify_with_obsidian_action(
    title: str,
    body: str,
    obsidian_path: str | Path | None = None,
) -> None:
    """
    Кликабельное уведомление с кнопкой «Открыть Obsidian».
    Использует notify-send --action + --activation-token-fd (≥0.8).
    activation token → Obsidian получает фокус на Wayland без dock-мерцания.
    """
    import urllib.parse

    path_str = str(obsidian_path) if obsidian_path else None

    clicked, activation_token = await _notify_send_with_action(title, body)

    if not clicked:
        return

    # Передаём activation token в окружение xdg-open.
    # xdg-open → gio open → Obsidian получает токен через XDG activation protocol
    # и сам поднимает своё окно (Electron 20+ поддерживает xdg-activation).
    env = dict(os.environ)
    if activation_token:
        env["XDG_ACTIVATION_TOKEN"] = activation_token
        env["DESKTOP_STARTUP_ID"]   = activation_token
        logger.info("notifier: activation token есть — Obsidian должен получить фокус")
    else:
        logger.debug("notifier: activation token отсутствует — фокус на Wayland недоступен")

    if path_str:
        encoded = urllib.parse.quote(path_str, safe="")
        uri = f"obsidian://open?path={encoded}"
    else:
        uri = "obsidian://"

    try:
        proc = await asyncio.create_subprocess_exec(
            "xdg-open", uri,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        logger.info("notifier: Obsidian URI отправлен → %s", uri[:80])
    except FileNotFoundError:
        logger.debug("notifier: xdg-open не найден")
    except Exception as e:
        logger.debug("notifier: xdg-open ошибка — %s", e)


async def notify_tasks(
    tasks: list[dict],
    prologue: str = "",
    trigger: str = "",
    obsidian_path: str | Path | None = None,
    title: str = "",
) -> None:
    """
    Краткое уведомление: сколько задач ждёт в Obsidian.
    Если задан obsidian_path — кликабельное (через XDG portal):
    клик открывает файл в Obsidian и поднимает окно через activation token.
    """
    if not tasks:
        return

    total = len(tasks)
    high  = sum(1 for t in tasks if t.get("priority") == "HIGH")

    if total == 1:
        count_str = "1 задача"
    elif 2 <= total <= 4:
        count_str = f"{total} задачи"
    else:
        count_str = f"{total} задач"

    notif_title = title if title else "🤖 PC Assistant"
    body  = f"Сегодня {count_str} ждут в Obsidian"
    if high:
        body += f" — {high} срочных"

    if obsidian_path:
        # Кликабельное уведомление — fire-and-forget (внутри ждёт клик до 120с)
        # Сохраняем reference в module-level set чтобы GC не убил task
        task = asyncio.create_task(
            notify_with_obsidian_action(notif_title, body, obsidian_path=obsidian_path),
            name="notify_tasks_clickable",
        )
        _pending_notify_tasks.add(task)
        task.add_done_callback(_pending_notify_tasks.discard)
    else:
        await notify(notif_title, body, urgency="normal", timeout_ms=10000, icon="appointment-new")


# Хранилище activated кликабельных уведомлений — strong reference от GC
_pending_notify_tasks: set[asyncio.Task] = set()


async def notify_obsidian_written(path: str, note_type: str = "заметка") -> None:
    """Не используется в FS-режиме — Obsidian открывается напрямую."""
    pass  # оставляем для совместимости, но не шлём лишнее уведомление


async def notify_error(message: str) -> None:
    """Уведомление об ошибке."""
    await notify(
        "⚠️ PC Assistant",
        _short(message, 80),
        urgency="critical",
        timeout_ms=15000,
        icon="dialog-error",
    )


async def notify_check_task(task_title: str, delay_min: int = 30) -> None:
    """
    Напоминание: похоже задача выполнена — проверь и отметь.
    Отправляется немедленно, авто-завершение через delay_min минут.
    """
    await notify(
        "⏳ Похоже это уже готово",
        f"{_short(task_title)}\n"
        f"Отметь в Obsidian, или через {delay_min} мин отмечу сам",
        urgency="normal",
        timeout_ms=15000,
        icon="dialog-question",
    )


async def notify_auto_completed(task_title: str) -> None:
    """Уведомление: задача автоматически отмечена ассистентом."""
    await notify(
        "✅ Задача отмечена ассистентом",
        f"{_short(task_title)}\n"
        "Можешь отменить — просто сними галочку в Obsidian",
        urgency="normal",
        timeout_ms=12000,
        icon="emblem-default",
    )


# ─── Открытие Obsidian ───────────────────────────────────────────────────────

async def open_obsidian_note(fs_path: str | Path) -> None:
    """
    Открыть заметку в Obsidian.

    Стратегия зависит от окружения:
    - Всегда: xdg-open obsidian://open?path=... (с правильным URL-encoding)
    - X11: wmctrl поднимает и перемещает окно на второй монитор
    - Wayland/GNOME: xdotool / фокус через nативный Wayland (лучшее из доступного)
    """
    import urllib.parse
    path = str(fs_path)

    # 1. Открываем через URI — путь обязательно экранируем (пробелы, кириллица)
    encoded = urllib.parse.quote(path, safe="")
    uri = f"obsidian://open?path={encoded}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdg-open", uri,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        logger.info("notifier: Obsidian URI отправлен → %s", path)
    except FileNotFoundError:
        logger.debug("notifier: xdg-open не найден")
        return
    except Exception as e:
        logger.debug("notifier: xdg-open ошибка — %s", e)
        return

    # На X11 — пробуем поднять окно через wmctrl
    # На Wayland — фокус без токена невозможен; файл открыт, этого достаточно
    if not os.environ.get("WAYLAND_DISPLAY"):
        await asyncio.sleep(1.5)
        await _focus_obsidian_x11()


async def _focus_obsidian_x11() -> None:
    """Фокус + перемещение на второй монитор через wmctrl (X11)."""
    monitors = await _get_monitors()
    if len(monitors) >= 2:
        m = monitors[1]
        cmd = ["wmctrl", "-r", "Obsidian", "-e",
               f"0,{m['x']},{m['y']},{m['w']},{m['h']}"]
    else:
        cmd = ["wmctrl", "-a", "Obsidian"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3)
        logger.info("notifier: wmctrl: %s", " ".join(cmd))
    except FileNotFoundError:
        logger.debug("notifier: wmctrl не установлен")
    except Exception as e:
        logger.debug("notifier: wmctrl ошибка — %s", e)


async def _focus_obsidian_wayland() -> None:
    """
    Фокус Obsidian на Wayland (GNOME).
    Obsidian — snap/Electron — может работать через XWayland или нативный Wayland.
    Пробуем последовательно несколько методов.
    """
    # Метод 1: xdotool (работает если Obsidian через XWayland)
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "search", "--classname", "obsidian",
            "windowactivate", "--sync",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ret = await asyncio.wait_for(proc.wait(), timeout=3)
        if ret == 0:
            logger.info("notifier: xdotool активировал Obsidian")
            return
    except FileNotFoundError:
        logger.debug("notifier: xdotool не установлен")
    except Exception:
        pass

    # Метод 2: xdotool по имени окна
    try:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "search", "--name", "Obsidian",
            "windowactivate", "--sync",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        ret = await asyncio.wait_for(proc.wait(), timeout=3)
        if ret == 0:
            logger.info("notifier: xdotool (по имени) активировал Obsidian")
            return
    except Exception:
        pass

    # Метод 3: wmctrl с явным DISPLAY (XWayland-окна могут быть видны)
    try:
        env = {**os.environ, "DISPLAY": ":0"}
        proc = await asyncio.create_subprocess_exec(
            "wmctrl", "-a", "Obsidian",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3)
        logger.debug("notifier: wmctrl -a Obsidian (через :0)")
    except Exception:
        pass

    # На Wayland авто-фокус недоступен без разрешения пользователя.
    # Уведомление с кнопкой уже отправлено через notify_tasks — дублировать не нужно.
    logger.debug("notifier: Wayland — авто-фокус недоступен, используем уведомление из notify_tasks")


async def _get_monitors() -> list[dict]:
    """Список мониторов через xrandr --listmonitors."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "xrandr", "--listmonitors",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        monitors = []
        for line in stdout.decode().splitlines()[1:]:
            m = re.search(
                r'\d+:\s+\S+\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)',
                line,
            )
            if m:
                monitors.append({
                    "w": int(m.group(1)), "h": int(m.group(2)),
                    "x": int(m.group(3)), "y": int(m.group(4)),
                    "name": m.group(5),
                })
        return monitors
    except Exception:
        return []
