"""
notifier.py — desktop-уведомления через notify-send + открытие Obsidian.
"""

import asyncio
import logging
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


# ─── Открытие Obsidian на втором мониторе ────────────────────────────────────

async def open_obsidian_note(fs_path: str | Path) -> None:
    """
    Открыть заметку в Obsidian и переместить окно на второй монитор (если есть).

    1. xdg-open obsidian://open?path=... — просит Obsidian открыть файл
    2. Короткая пауза — Obsidian реагирует на URI не мгновенно
    3. wmctrl — перемещает окно Obsidian на второй монитор (если два монитора)
    """
    path = str(fs_path)

    # 1. Открываем через URI-схему Obsidian
    try:
        uri = f"obsidian://open?path={path}"
        proc = await asyncio.create_subprocess_exec(
            "xdg-open", uri,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        logger.debug("notifier: открываем Obsidian → %s", path)
    except FileNotFoundError:
        logger.debug("notifier: xdg-open не найден")
        return
    except Exception as e:
        logger.debug("notifier: xdg-open ошибка — %s", e)
        return

    # 2. Ждём пока Obsidian откроется
    await asyncio.sleep(1.5)

    # 3. Перемещаем на второй монитор если он есть
    monitors = await _get_monitors()
    if len(monitors) >= 2:
        m = monitors[1]
        try:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-r", "Obsidian",
                "-e", f"0,{m['x']},{m['y']},{m['w']},{m['h']}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3)
            logger.info(
                "notifier: Obsidian перемещён на %s (%dx%d+%d+%d)",
                m["name"], m["w"], m["h"], m["x"], m["y"],
            )
        except FileNotFoundError:
            logger.debug("notifier: wmctrl не установлен — sudo apt install wmctrl")
        except Exception as e:
            logger.debug("notifier: wmctrl ошибка — %s", e)
    else:
        # Один монитор — просто поднимаем окно на передний план
        try:
            proc = await asyncio.create_subprocess_exec(
                "wmctrl", "-a", "Obsidian",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            pass


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
