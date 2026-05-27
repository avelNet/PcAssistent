"""
notifier.py — отправка уведомлений на рабочий стол через notify-send.

Используется вместо голоса когда piper не настроен,
и всегда — для показа результата записи в Obsidian.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Иконки по приоритету
_PRIORITY_ICON = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}


async def notify(
    title: str,
    body: str = "",
    urgency: str = "normal",   # low | normal | critical
    timeout_ms: int = 8000,
    icon: str = "dialog-information",
) -> None:
    """
    Отправить desktop-уведомление через notify-send.
    Не бросает исключений — ошибка только в лог.
    """
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
    """Уведомление с результатами LLM — задачи дня."""
    if not tasks:
        return

    trigger_labels = {
        "morning_briefing": "☀️ Доброе утро",
        "after_work_session": "🏁 Сессия завершена",
        "user_returned": "👋 С возвращением",
        "evening_summary": "🌙 Итог дня",
        "manual": "🔍 Анализ",
    }
    title = trigger_labels.get(trigger, "🤖 PC Assistant")

    # Топ-3 задачи HIGH → MED → LOW
    sorted_tasks = sorted(
        tasks,
        key=lambda t: {"HIGH": 0, "MED": 1, "LOW": 2}.get(t.get("priority", "LOW"), 3)
    )
    lines = []
    for t in sorted_tasks[:3]:
        icon = _PRIORITY_ICON.get(t.get("priority", "LOW"), "⚪")
        lines.append(f"{icon} {t['title']}")

    total = len(tasks)
    if total > 3:
        lines.append(f"  ...и ещё {total - 3}")

    body = "\n".join(lines)

    urgency = "critical" if any(t.get("priority") == "HIGH" for t in tasks[:2]) else "normal"
    await notify(title, body, urgency=urgency, timeout_ms=12000, icon="appointment-new")


async def notify_obsidian_written(path: str, note_type: str = "заметка") -> None:
    """Уведомление что ассистент записал что-то в Obsidian."""
    await notify(
        f"📝 Obsidian — {note_type} обновлена",
        path,
        urgency="low",
        timeout_ms=6000,
        icon="text-editor",
    )


async def notify_error(message: str) -> None:
    """Уведомление об ошибке."""
    await notify(
        "⚠️ PC Assistant",
        message,
        urgency="critical",
        timeout_ms=15000,
        icon="dialog-error",
    )
