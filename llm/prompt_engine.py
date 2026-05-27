"""
prompt_engine.py — формирует промпты для LLM под каждый тип триггера.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── Системный промпт (общий для всех триггеров) ────────────────────────────

SYSTEM_PROMPT = """Ты — персональный ассистент разработчика. Работаешь локально, все данные конфиденциальны.

Твоя задача: проанализировать контекст и выдать список конкретных задач на день.

ФОРМАТ ОТВЕТА — СТРОГО ТАК:
Сначала 2-3 предложения анализа. Затем каждая задача — ОТДЕЛЬНОЙ СТРОКОЙ, без нумерации, без тире, без markdown:

TASK: название | ПРИОРИТЕТ | проект | описание

ПРИОРИТЕТ — только HIGH, MED или LOW.

ПРИМЕР (точно копируй этот стиль, без тире и без звёздочек):

Сервис аутентификации почти готов, осталось закрыть PR и написать тесты.

TASK: Написать тесты для AuthService | HIGH | Tap2Go | Покрыть register и login методы
TASK: Ревью PR #15 от коллеги | HIGH | Tap2Go | Висит уже 2 дня
TASK: Изучить Redis Streams | MED | Tap2Go | Нужно для очереди уведомлений
TASK: Обновить README | LOW | PcAssistent | Добавить раздел установки

ЗАПРЕЩЕНО:
- НЕ пиши "- TASK:" (без тире перед TASK)
- НЕ пиши "**TASK:**" (без звёздочек)
- НЕ нумеруй задачи (1. 2. 3.)
- НЕ используй ### заголовки
- Пиши только на русском языке

ПРАВИЛО ПРИОРИТЕТОВ:
Если в контексте есть Kanban — строго соблюдай его.
НЕ предлагай задачи из раздела "Не начато" пока есть незавершённые из "В работе".
"""

# ─── Шаблоны промптов по триггерам ──────────────────────────────────────────

def build(trigger: str, context: dict) -> tuple[str, str]:
    """
    Сформировать (system_prompt, user_prompt) для заданного триггера.
    """
    builders = {
        "morning_briefing": _morning_briefing,
        "after_work_session": _after_work_session,
        "user_returned": _user_returned,
        "errors_spike": _errors_spike,
        "evening_summary": _evening_summary,
        "manual": _manual,
    }

    builder = builders.get(trigger, _manual)
    user_prompt = builder(context)

    # Добавляем текущее время в начало каждого промпта — LLM должен знать когда это
    now = datetime.now()
    _MONTHS_RU = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая",    6: "июня",    7: "июля",   8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    weekdays_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    time_str = now.strftime("%H:%M")
    date_str = f"{now.day} {_MONTHS_RU[now.month]} {now.year}"
    weekday_str = weekdays_ru[now.weekday()]
    hour = now.hour
    if 5 <= hour < 12:
        time_of_day = "утро"
    elif 12 <= hour < 17:
        time_of_day = "день"
    elif 17 <= hour < 22:
        time_of_day = "вечер"
    else:
        time_of_day = "ночь"

    time_header = f"🕐 Сейчас {time_str}, {weekday_str}, {date_str} ({time_of_day}).\n\n"
    user_prompt = time_header + user_prompt

    # Сообщение от пользователя из консольного чата — идёт первым
    user_msg = context.get("user_message", "").strip()
    if user_msg:
        user_prompt = f"💬 Сообщение от пользователя:\n{user_msg}\n\n" + user_prompt

    # Если установлен фокус — добавляем жёсткое ограничение по проекту
    focus = context.get("focus")
    if focus:
        user_prompt += (
            f"\n\n🎯 РЕЖИМ ФОКУСА АКТИВЕН: работаем ТОЛЬКО с проектом «{focus}».\n"
            f"Генерируй задачи исключительно по этому проекту. "
            f"Остальные проекты — не упоминай и не предлагай."
        )

    # Явное напоминание о формате в конце каждого промпта
    user_prompt += "\n\nНапомню: каждая задача ОБЯЗАТЕЛЬНО начинается с TASK: и содержит 4 поля через |"

    logger.debug("prompt_engine: триггер='%s', фокус='%s', длина промпта=%d символов",
                 trigger, focus or "нет", len(user_prompt))

    return SYSTEM_PROMPT, user_prompt


def _morning_briefing(ctx: dict) -> str:
    today = date.today().strftime("%d %B %Y")
    parts = [f"📅 Утренний брифинг — {today}\n"]

    parts.append(_section_kanban(ctx))
    parts.append(_section_obsidian(ctx))
    parts.append(_section_git(ctx))
    parts.append(_section_errors(ctx))
    parts.append(_section_history(ctx))
    parts.append(_section_ide(ctx))

    parts.append("""
На основе этих данных:
1. Напомни коротко что было вчера (2-3 предложения)
2. Сформируй план на сегодня: 5-7 задач
3. Учти незакрытые задачи — повысь им приоритет
""")
    return "\n".join(p for p in parts if p.strip())


def _after_work_session(ctx: dict) -> str:
    session = ctx.get("session", {})
    duration = session.get("duration_min", 0)
    parts = [f"🏁 Конец рабочей сессии (длительность: {duration:.0f} мин)\n"]

    parts.append(_section_git(ctx))
    parts.append(_section_errors(ctx))
    parts.append(_section_ide(ctx))

    parts.append("""
На основе этих данных:
1. Кратко опиши что было сделано в этой сессии (1-2 предложения)
2. Определи следующие шаги: 3-4 задачи на продолжение работы
""")
    return "\n".join(p for p in parts if p.strip())


def _user_returned(ctx: dict) -> str:
    idle_min = ctx.get("idle_was_min", 0)
    idle_h = idle_min // 60
    idle_m = idle_min % 60
    idle_str = f"{idle_h}ч {idle_m}мин" if idle_h else f"{idle_m}мин"

    parts = [f"👋 Возвращение после перерыва ({idle_str})\n"]

    parts.append(_section_kanban(ctx))
    parts.append(_section_obsidian(ctx))
    parts.append(_section_git(ctx))
    parts.append(_section_ide(ctx))
    parts.append(_section_today_tasks(ctx))

    parts.append("""
На основе этих данных:
1. Напомни контекст — где остановился до перерыва (2-3 предложения)
2. Предложи с чего начать: 3-5 задач для плавного входа в работу
""")
    return "\n".join(p for p in parts if p.strip())


def _errors_spike(ctx: dict) -> str:
    spike = ctx.get("errors_spike", {})
    error_type = spike.get("type", "Unknown")
    error_file = spike.get("file", "неизвестный файл")
    count = spike.get("count", 0)

    parts = [f"⚠️ Спайк ошибок: {error_type} × {count} раз в {error_file}\n"]
    parts.append(_section_errors(ctx))
    parts.append(_section_git(ctx))

    parts.append(f"""
Проанализируй паттерн ошибок {error_type} в файле {error_file}:
1. Объясни вероятную причину (2-3 предложения)
2. Предложи решение: 2-3 конкретные задачи
""")
    return "\n".join(p for p in parts if p.strip())


def _evening_summary(ctx: dict) -> str:
    today = date.today().strftime("%d %B %Y")
    parts = [f"🌙 Вечерний итог — {today}\n"]

    parts.append(_section_productivity(ctx))
    parts.append(_section_git(ctx))
    parts.append(_section_errors(ctx))
    parts.append(_section_today_tasks(ctx))

    parts.append("""
На основе этих данных:
1. Подведи итог дня: что сделано, что нет (3-4 предложения)
2. Задачи на завтра: 3-5 задач (LOW приоритет если не срочно)
""")
    return "\n".join(p for p in parts if p.strip())


def _manual(ctx: dict) -> str:
    today = date.today().strftime("%d %B %Y")
    parts = [f"🔍 Полный анализ — {today}\n"]

    parts.append(_section_kanban(ctx))
    parts.append(_section_obsidian(ctx))
    parts.append(_section_git(ctx))
    parts.append(_section_ide(ctx))
    parts.append(_section_errors(ctx))
    parts.append(_section_productivity(ctx))
    parts.append(_section_history(ctx))
    parts.append(_section_today_tasks(ctx))

    parts.append("""
На основе всех данных:
1. Опиши текущее состояние проектов (3-4 предложения)
2. Сформируй полный список задач: 5-8 задач
""")
    return "\n".join(p for p in parts if p.strip())


# ─── Секции контекста ────────────────────────────────────────────────────────

def _section_git(ctx: dict) -> str:
    git_data = ctx.get("git", [])
    if not git_data:
        return ""

    lines = ["## Git репозитории"]
    for repo in git_data[:5]:  # максимум 5 репо
        name = repo.get("name", "unknown")
        branch = repo.get("branch", "?")
        uncommitted = repo.get("uncommitted_count", 0)
        last = repo.get("last_commit", {})

        lines.append(f"\n### {name} [{branch}]")
        if uncommitted:
            lines.append(f"  Незакоммиченных файлов: {uncommitted}")
        if last:
            lines.append(f"  Последний коммит: {last.get('hash', '')} — {last.get('message', '')}")
            lines.append(f"  Время: {last.get('time', '')}")

        recent = repo.get("recent_log", "")
        if recent:
            lines.append("  Последние коммиты:")
            for log_line in recent.splitlines()[:5]:
                lines.append(f"    {log_line}")

        todos = repo.get("todos", [])
        if todos:
            lines.append(f"  TODO/FIXME ({len(todos)}):")
            for todo in todos[:5]:
                lines.append(f"    {todo}")

    return "\n".join(lines) + "\n"


def _section_errors(ctx: dict) -> str:
    errors = ctx.get("errors", [])
    if not errors:
        return ""

    lines = [f"## Ошибки (за последние 24ч, топ {min(len(errors), 10)})"]
    for err in errors[:10]:
        count = err.get("count", 1)
        error_type = err.get("error_type", "Unknown")
        file_ = err.get("file", "?")
        line_ = err.get("line", "?")
        msg = err.get("message", "")[:100]
        source = err.get("source", "?")
        lines.append(f"  [{source}] {error_type} × {count} — {file_}:{line_}")
        if msg:
            lines.append(f"    {msg}")

    return "\n".join(lines) + "\n"


def _section_ide(ctx: dict) -> str:
    ide = ctx.get("ide", {})
    if not ide:
        return ""

    lines = ["## IDE (JetBrains)"]

    # projects — список dict {name, path} или строк (legacy)
    projects = ide.get("projects", [])
    if projects:
        names = [
            p["name"] if isinstance(p, dict) else str(p)
            for p in projects[:5]
        ]
        lines.append("  Проекты: " + ", ".join(names))

    open_files = ide.get("open_files", [])
    if open_files:
        lines.append("  Открытые файлы:")
        for f in open_files[:10]:
            lines.append(f"    {f}")

    breakpoints = ide.get("breakpoints", [])
    if breakpoints:
        lines.append(f"  Breakpoints ({len(breakpoints)}) — вероятно проблемные места:")
        for bp in breakpoints[:5]:
            if isinstance(bp, dict):
                lines.append(f"    {bp.get('file', '')}:{bp.get('line', '')}")
            else:
                lines.append(f"    {bp}")

    return "\n".join(lines) + "\n"


def _section_productivity(ctx: dict) -> str:
    prod = ctx.get("productivity", {})
    if not prod:
        return ""

    lines = ["## Продуктивность сегодня"]
    if prod.get("deep_work_hours"):
        lines.append(f"  Глубокая работа: {prod['deep_work_hours']:.1f}ч")
    if prod.get("peak_hour"):
        lines.append(f"  Пик активности: {prod['peak_hour']}")
    if prod.get("switches"):
        lines.append(f"  Переключений окон: {prod['switches']}")
    if prod.get("commits"):
        lines.append(f"  Коммитов: {prod['commits']}")

    return "\n".join(lines) + "\n"


def _section_history(ctx: dict) -> str:
    history = ctx.get("history", [])
    if not history:
        return ""

    # Группируем по дате
    by_date: dict[str, list] = {}
    for task in history:
        d = task.get("date", "?")
        by_date.setdefault(d, []).append(task)

    lines = ["## История задач (последние 3 дня)"]
    for day_date in sorted(by_date.keys(), reverse=True)[:3]:
        day_tasks = by_date[day_date]
        done = sum(1 for t in day_tasks if t.get("done"))
        total = len(day_tasks)
        lines.append(f"\n  {day_date}: {done}/{total} выполнено")
        for t in day_tasks:
            status = "✓" if t.get("done") else "○"
            lines.append(f"    {status} [{t.get('priority','?')}] {t.get('title','')}")

    return "\n".join(lines) + "\n"


def _section_kanban(ctx: dict) -> str:
    """Статус проектов из канбан-досок — всегда идёт первым."""
    kanban = ctx.get("obsidian", {}).get("kanban", {})
    if not kanban:
        return ""

    lines = ["## 📋 Kanban — ДОПУСТИМЫЕ ЗАДАЧИ"]

    for project, columns in kanban.items():
        in_progress = columns.get("in_progress", [])
        not_started = columns.get("not_started", [])
        done = columns.get("done", [])

        lines.append(f"\n### {project}")

        if in_progress:
            lines.append("✅ РАЗРЕШЕНО — только эти пункты (они 'В работе'):")
            for item in in_progress:
                lines.append(f"  ✔ {item}")
        else:
            lines.append("(нет активных задач — предлагай из 'Не начато')")

        if done:
            lines.append("🏁 Уже готово (не предлагать):")
            for item in done:
                lines.append(f"  - {item}")

        if not_started:
            blocked = ", ".join(not_started[:5])
            if len(not_started) > 5:
                blocked += f" и ещё {len(not_started) - 5}"
            lines.append(f"🚫 ЗАБЛОКИРОВАНО — не предлагать пока есть незавершённые выше: {blocked}")

    lines.append("\n⚠️ ПРАВИЛО: предлагай задачи ТОЛЬКО по пунктам с ✔. Всё остальное — запрещено.")

    return "\n".join(lines) + "\n"


def _section_obsidian(ctx: dict) -> str:
    """Заметки из Obsidian vault — только по проектам 'В работе'."""
    obsidian = ctx.get("obsidian", {})
    projects = obsidian.get("projects", {})
    kanban = obsidian.get("kanban", {})
    if not projects:
        return ""

    # Определяем что сейчас "в работе" по канбану
    in_progress_items: set[str] = set()
    for cols in kanban.values():
        for item in cols.get("in_progress", []):
            # Нормализуем: "Сервис 01 — Инфраструктура" → ищем похожие пути
            in_progress_items.add(item.lower())

    lines = ["## Obsidian — заметки по проектам"]

    for project_name, notes in sorted(projects.items()):
        lines.append(f"\n### 📁 {project_name}")
        for note in notes:
            path = note.get("path", "")
            short_path = path.replace(f"{project_name}/", "", 1)
            days_ago = note.get("modified_days_ago", 0)
            age = "сегодня" if days_ago < 1 else f"{int(days_ago)}д назад"

            # Если есть канбан — помечаем статус заметки
            status_tag = ""
            if in_progress_items:
                note_name = Path(short_path).stem.lower()
                is_wip = any(
                    note_name in item or item in note_name
                    for item in in_progress_items
                )
                status_tag = " [🔄 В РАБОТЕ]" if is_wip else " [📋 не начато]"

            lines.append(f"\n#### {short_path} ({age}){status_tag}")
            content = note.get("content", "").strip()
            if content:
                lines.append(content)

    return "\n".join(lines) + "\n"


def _section_today_tasks(ctx: dict) -> str:
    tasks = ctx.get("today_tasks", [])
    if not tasks:
        return ""

    done = sum(1 for t in tasks if t.get("done"))
    total = len(tasks)
    lines = [f"## Задачи сегодня ({done}/{total} выполнено)"]

    for t in tasks:
        status = "✓" if t.get("done") else "○"
        lines.append(f"  {status} [{t.get('priority','?')}] {t.get('title','')}")

    return "\n".join(lines) + "\n"
