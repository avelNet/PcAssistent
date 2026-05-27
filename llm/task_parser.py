"""
task_parser.py — парсит сырой ответ LLM и извлекает структурированные задачи.

Формат строки задачи в ответе:
    TASK: title | HIGH|MED|LOW | project | description

Пример:
    TASK: Закрыть PR #42 | HIGH | backend | Не забыть обновить тесты
    TASK: Разобраться с Redis TTL | MED | cache | Посмотреть доки по expire
"""

import logging
import re
import uuid
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Паттерн: TASK: всё что угодно, разделённое |
# Допускаем leading whitespace/bullets: "  - TASK:", "* TASK:", "**TASK:**"
_TASK_RE = re.compile(
    r"^[*\-\s]*\*{0,2}TASK:\*{0,2}\s*(.+?)\s*\|\s*(HIGH|MED|LOW)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Запасной паттерн: TASK: без pipe-разделителей (только заголовок)
_TASK_SIMPLE_RE = re.compile(
    r"^[*\-\s]*\*{0,2}TASK:\*{0,2}\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

_VALID_PRIORITIES = {"HIGH", "MED", "LOW"}


def parse(raw_text: str) -> dict:
    """
    Разобрать ответ LLM.
    Возвращает { "tasks": [...], "prologue": str }
    """
    tasks = []
    prologue = ""

    if not raw_text or not raw_text.strip():
        logger.warning("task_parser: получен пустой ответ от LLM")
        return {"tasks": [], "prologue": ""}

    today = date.today().isoformat()
    now = datetime.utcnow().isoformat()

    # Извлекаем prologue — весь текст до первой строки TASK:
    first_task_pos = raw_text.upper().find("TASK:")
    if first_task_pos > 0:
        prologue = raw_text[:first_task_pos].strip()
    elif first_task_pos == -1:
        prologue = raw_text.strip()

    # Парсим строки TASK: с полным форматом
    for match in _TASK_RE.finditer(raw_text):
        title, priority, project, description = (
            match.group(1).strip(),
            match.group(2).upper().strip(),
            match.group(3).strip(),
            match.group(4).strip(),
        )

        if priority not in _VALID_PRIORITIES:
            priority = "MED"

        tasks.append({
            "id": str(uuid.uuid4()),
            "title": title,
            "priority": priority,
            "project": project or None,
            "description": description or None,
            "done": False,
            "created_at": now,
            "date": today,
        })

    # Если полный формат не сработал — пробуем упрощённый (только заголовок)
    if not tasks:
        logger.warning("task_parser: полный формат TASK не найден, пробуем упрощённый")
        for match in _TASK_SIMPLE_RE.finditer(raw_text):
            title = match.group(1).strip()
            # Убираем возможные pipe-символы в конце
            title = title.split("|")[0].strip()
            if title:
                tasks.append({
                    "id": str(uuid.uuid4()),
                    "title": title,
                    "priority": "MED",
                    "project": None,
                    "description": None,
                    "done": False,
                    "created_at": now,
                    "date": today,
                })

    if tasks:
        logger.info("task_parser: извлечено %d задач", len(tasks))
        for t in tasks:
            logger.debug("  [%s] %s — %s", t["priority"], t["title"], t.get("project", "—"))
    else:
        logger.warning("task_parser: задачи не найдены в ответе LLM")
        logger.debug("--- Ответ LLM ---\n%s\n---", raw_text[:500])

    return {"tasks": tasks, "prologue": prologue}


def format_tasks_for_display(tasks: list[dict]) -> str:
    """Форматировать задачи для вывода в консоль/лог."""
    if not tasks:
        return "Задачи не сгенерированы"

    priority_emoji = {"HIGH": "🔴", "MED": "🟡", "LOW": "🟢"}
    lines = []
    for i, t in enumerate(tasks, 1):
        emoji = priority_emoji.get(t["priority"], "⚪")
        project = f" [{t['project']}]" if t.get("project") else ""
        lines.append(f"{i}. {emoji} {t['title']}{project}")
        if t.get("description"):
            lines.append(f"   └─ {t['description']}")

    return "\n".join(lines)
