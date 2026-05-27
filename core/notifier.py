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


async def notify_with_obsidian_action(
    title: str,
    body: str,
    obsidian_path: str | Path | None = None,
) -> None:
    """
    Уведомление с кнопкой «Открыть Obsidian».
    При клике — фокусирует Obsidian через gtk-launch (работает на Wayland).
    """
    try:
        cmd = [
            "notify-send",
            "--urgency", "normal",
            "--expire-time", "15000",
            "--icon", "appointment-new",
            "--action", "open:Открыть Obsidian",
            title,
            body,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            clicked = stdout.decode().strip() == "open"
        except asyncio.TimeoutError:
            proc.kill()
            clicked = False

        if clicked:
            # Открываем файл если передан путь, потом фокусируем
            if obsidian_path:
                import urllib.parse
                encoded = urllib.parse.quote(str(obsidian_path), safe="")
                uri_proc = await asyncio.create_subprocess_exec(
                    "xdg-open", f"obsidian://open?path={encoded}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(uri_proc.wait(), timeout=5)
                await asyncio.sleep(0.8)

            # Фокусируем окно
            await asyncio.create_subprocess_exec(
                "gtk-launch", "obsidian_obsidian",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("notifier: пользователь кликнул → открываем Obsidian")

    except FileNotFoundError:
        logger.debug("notifier: notify-send не найден")
    except Exception as e:
        logger.debug("notifier: notify_with_obsidian_action ошибка — %s", e)


async def notify_tasks(tasks: list[dict], prologue: str = "", trigger: str = "") -> None:
    """
    Одно аккуратное уведомление с задачами дня.
    Показывает 2 самые важные задачи (сокращённо) + счётчик остальных.
    """
    if not tasks:
        return

    trigger_labels = {
        "morning_briefing":  "☀️ Утренний брифинг",
        "after_work_session": "🏁 Сессия завершена",
        "user_returned":     "👋 С возвращением",
        "evening_summary":   "🌙 Итог дня",
        "manual":            "🤖 Анализ готов",
    }
    label = trigger_labels.get(trigger, "🤖 PC Assistant")
    total = len(tasks)
    title = f"{label} — {total} {'задача' if total == 1 else 'задачи' if 2 <= total <= 4 else 'задач'}"

    # Топ-2 HIGH → MED → LOW, сокращённо
    sorted_tasks = sorted(
        tasks,
        key=lambda t: {"HIGH": 0, "MED": 1, "LOW": 2}.get(t.get("priority", "LOW"), 3)
    )
    lines = []
    for t in sorted_tasks[:2]:
        icon = _PRIORITY_ICON.get(t.get("priority", "LOW"), "⚪")
        lines.append(f"{icon} {_short(t['title'])}")

    if total > 2:
        lines.append(f"  + ещё {total - 2}")

    body = "\n".join(lines)
    has_high = any(t.get("priority") == "HIGH" for t in sorted_tasks[:2])
    urgency = "normal" if not has_high else "normal"  # critical мешает автоскрытию

    await notify(title, body, urgency=urgency, timeout_ms=10000, icon="appointment-new")


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
