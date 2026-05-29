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


def _notify_portal_blocking(
    title: str,
    body: str,
    obsidian_path: str | None,
) -> tuple[bool, str | None, bool]:
    """
    Уведомление через XDG Desktop Portal (D-Bus).
    На GNOME 43+: ActionInvoked содержит activation-token → Obsidian может получить фокус.
    Возвращает (clicked, activation_token).
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception as e:
        logger.debug("notifier: gi.repository.Gio недоступен — %s", e)
        return False, None, False

    clicked = []
    token: list[str] = []
    loop = GLib.MainLoop()
    sub_id: list[int] = []

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception as e:
        logger.debug("notifier: не удалось подключиться к session bus — %s", e)
        return False, None, False

    notif_id = "pc-assistant-focus"

    def on_action_invoked(conn, sender, obj_path, iface, sig, params, _):
        try:
            vals = params.unpack()
            # vals = (id, action) или (id, action, {extra})
            if len(vals) >= 2 and vals[0] == notif_id:
                clicked.append(True)
                if len(vals) >= 3 and isinstance(vals[2], dict):
                    t = vals[2].get("activation-token")
                    if t:
                        token.append(t)
        except Exception:
            pass
        loop.quit()

    sub_id.append(bus.signal_subscribe(
        None,
        "org.freedesktop.portal.Notification",
        "ActionInvoked",
        "/org/freedesktop/portal/desktop",
        None,
        Gio.DBusSignalFlags.NONE,
        on_action_invoked,
        None,
    ))

    # Строим уведомление как Python-dict (GLib создаёт Variant рекурсивно)
    notification_dict = {
        "title":    GLib.Variant("s", title),
        "body":     GLib.Variant("s", body),
        "priority": GLib.Variant("s", "high"),
        # icon обязателен — без него GNOME ищет приложение по D-Bus sender (:1.NNN),
        # не находит .desktop и создаёт временную запись в доке (мерцание)
        "icon":     GLib.Variant("(sv)", ("themed-icon", GLib.Variant("as", ["appointment-new"]))),
        "default-action-target": GLib.Variant("s", "pc-assistant"),
        "buttons":  GLib.Variant("aa{sv}", [
            {
                "label":  GLib.Variant("s", "Открыть Obsidian"),
                "action": GLib.Variant("s", "open"),
            }
        ]),
    }

    try:
        bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Notification",
            "AddNotification",
            GLib.Variant("(sa{sv})", (notif_id, notification_dict)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except Exception as e:
        logger.debug("notifier: portal AddNotification ошибка — %s", e)
        if sub_id:
            bus.signal_unsubscribe(sub_id[0])
        return False, None, False  # портал недоступен — нужен fallback

    GLib.timeout_add_seconds(120, loop.quit)
    loop.run()

    if sub_id:
        bus.signal_unsubscribe(sub_id[0])

    # Убираем уведомление
    try:
        bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Notification",
            "RemoveNotification",
            GLib.Variant("(s)", (notif_id,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
    except Exception:
        pass

    # shown=True: уведомление было показано (даже если не кликнули)
    return bool(clicked), token[0] if token else None, True


def _notify_blocking(
    title: str,
    body: str,
    obsidian_path: str | None,
) -> bool:
    """
    Fallback через gi.repository.Notify (libnotify).
    Используется если XDG Portal недоступен.
    """
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify, GLib
    except Exception as e:
        logger.debug("notifier: gi.repository.Notify недоступен — %s", e)
        return False

    clicked = []

    Notify.init("pc-assistant")
    notif = Notify.Notification.new(title, body, "appointment-new")
    notif.set_urgency(Notify.Urgency.CRITICAL)
    notif.set_timeout(Notify.EXPIRES_NEVER)

    loop = GLib.MainLoop()

    def on_action(notification, action_name, user_data):
        clicked.append(True)
        loop.quit()

    def on_closed(notification):
        loop.quit()

    notif.add_action("open", "Открыть Obsidian", on_action, None)
    notif.connect("closed", on_closed)

    try:
        notif.show()
    except Exception as e:
        logger.debug("notifier: notif.show() ошибка — %s", e)
        return False

    GLib.timeout_add_seconds(120, loop.quit)
    loop.run()

    return bool(clicked)


async def notify_with_obsidian_action(
    title: str,
    body: str,
    obsidian_path: str | Path | None = None,
) -> None:
    """
    Уведомление с кнопкой «Открыть Obsidian».
    Использует XDG Portal для получения activation token → Obsidian получает фокус на Wayland.
    """
    import urllib.parse

    path_str = str(obsidian_path) if obsidian_path else None

    # Пробуем portal (с activation token)
    clicked, activation_token, portal_shown = await asyncio.to_thread(
        _notify_portal_blocking, title, body, path_str
    )

    # Fallback на libnotify только если портал НЕ смог показать уведомление
    # (если показал но не кликнули — не показываем второе)
    if not portal_shown:
        clicked = await asyncio.to_thread(_notify_blocking, title, body, path_str)
        activation_token = None

    if not clicked:
        return

    # Открываем файл внутри Obsidian через URI
    if path_str:
        encoded = urllib.parse.quote(path_str, safe="")
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdg-open", f"obsidian://open?path={encoded}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # Фокусируем Obsidian через activation token (Wayland XDG activation)
    # Electron читает DESKTOP_STARTUP_ID / XDG_ACTIVATION_TOKEN и запрашивает фокус у композитора
    focus_env = dict(os.environ)
    if activation_token:
        focus_env["DESKTOP_STARTUP_ID"] = activation_token
        focus_env["XDG_ACTIVATION_TOKEN"] = activation_token
        logger.info("notifier: activation token получен — запрашиваем фокус Obsidian")

    for cmd in (
        ["snap", "run", "obsidian"],
        ["gtk-launch", "obsidian_obsidian"],
        ["obsidian"],
    ):
        try:
            await asyncio.create_subprocess_exec(
                *cmd,
                env=focus_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("notifier: Obsidian запущен через %s (token=%s)",
                        cmd[0], "да" if activation_token else "нет")
            break
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug("notifier: %s ошибка — %s", cmd[0], e)


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

    # 2. Пауза — Obsidian обрабатывает URI асинхронно
    await asyncio.sleep(1.5)

    # 3. Поднимаем окно на передний план
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))

    if is_wayland:
        await _focus_obsidian_wayland()
    else:
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

    # Метод 4: нативный Wayland — показываем уведомление с кнопкой «Открыть Obsidian»
    # Пользователь кликает → окно выходит на передний план (Wayland разрешает по клику)
    logger.debug("notifier: Wayland — авто-фокус недоступен, показываем кнопку")
    asyncio.create_task(
        notify_with_obsidian_action(
            "📝 Заметка записана в Obsidian",
            "Нажми чтобы открыть",
            obsidian_path=None,  # файл уже открыт через URI выше
        )
    )


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
