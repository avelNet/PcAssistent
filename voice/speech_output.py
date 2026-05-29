"""
voice/speech_output.py — высокоуровневый интерфейс озвучки.

Структура брифинга:
  1. Приветствие по триггеру
  2. Пролог (анализ LLM, ≤300 симв)
  3. Сводка: "У вас X срочных, Y важных и Z обычных задач."
  4. Рекомендация: "Предлагаю начать с самых срочных."
  5. Топ-3 задачи (каждая — отдельный speak, нет риска обрезки)
  6. Завершение: "Удачи! Работаю в фоне."
"""

import logging

from voice.tts_engine import TTSEngine
from voice.tts_preprocessor import preprocess_for_tts

logger = logging.getLogger(__name__)

_MAX_TASKS    = 3   # больше 3 не запомнить
_MAX_PROLOGUE = 280

_TRIGGER_GREETING = {
    "morning_briefing":    "Доброе утро! Задачи на сегодня.",
    "after_work_session":  "Сессия завершена. Вот что дальше.",
    "user_returned":       "С возвращением! Напоминаю контекст.",
    "evening_summary":     "Итог дня.",
    "manual":              "Готово.",
    "focus_switch":        "",  # у focus_switch свой заголовок — в title уведомления
}


def _priority_phrase(count: int, high_word: str, low_word: str) -> str:
    """'одна срочная' | '3 срочных'"""
    if count == 1:
        return f"одна {high_word}"
    return f"{count} {low_word}"


def _build_summary(high: int, med: int, low: int) -> str:
    """
    'У вас 2 срочных, 3 важных и одна обычная.'
    Если нет HIGH/MED (задачи без приоритета, напр. из чеклиста) —
    просто 'У вас N задач.' без слова 'обычных'.
    """
    total = high + med + low
    if not total:
        return ""

    # Нет приоритетных задач — не делаем вид что все "обычные", просто считаем
    if not high and not med:
        if total == 1:
            return "У вас одна задача."
        elif total in (2, 3, 4):
            return f"У вас {total} задачи."
        else:
            return f"У вас {total} задач."

    parts = []
    if high:
        parts.append(_priority_phrase(high, "срочная", "срочных"))
    if med:
        parts.append(_priority_phrase(med, "важная", "важных"))
    if low:
        parts.append(_priority_phrase(low, "обычная", "обычных"))

    if len(parts) == 1:
        return f"У вас {parts[0]}."
    return "У вас " + ", ".join(parts[:-1]) + " и " + parts[-1] + "."


def _recommendation(high: int, med: int) -> str:
    if high:
        return "Предлагаю начать с самых срочных."
    if med:
        return "Предлагаю начать с важных задач."
    return "Начнём с первой по списку."


class SpeechOutput:
    def __init__(self, config: dict):
        self.tts = TTSEngine(config)

    async def speak_tasks(
        self,
        tasks: list[dict],
        prologue: str = "",
        trigger: str = "",
    ) -> None:
        """
        Брифинг по задачам. Каждый логический блок озвучивается отдельным вызовом
        — Silero не обрезает текст при большом объёме.
        """
        if not self.tts.enabled or not tasks:
            return

        # Все фразы проходят через препроцессор: числа → слова, англ → фонетика
        async def speak(text: str) -> None:
            await self.tts.speak(preprocess_for_tts(text))

        # 1. Приветствие
        greeting = _TRIGGER_GREETING.get(trigger, "")
        if greeting:
            await speak(greeting)

        # 2. Пролог
        if prologue:
            short = prologue.strip()[:_MAX_PROLOGUE]
            await speak(preprocess_for_tts(short))

        # 3. Сортируем HIGH→MED→LOW
        sorted_tasks = sorted(
            tasks,
            key=lambda t: {"HIGH": 0, "MED": 1, "LOW": 2}.get(t.get("priority", "LOW"), 3),
        )
        total = len(tasks)
        top   = sorted_tasks[:_MAX_TASKS]

        high = sum(1 for t in tasks if t.get("priority") == "HIGH")
        med  = sum(1 for t in tasks if t.get("priority") == "MED")
        low  = total - high - med

        # 4. Сводка + рекомендация
        if total == 1:
            await speak("У вас одна задача.")
        else:
            summary = _build_summary(high, med, low)
            if summary:
                await speak(summary)
            await speak(_recommendation(high, med))

        # 5. Сами задачи — каждая отдельно, чтобы не было обрезки
        for task in top:
            title    = preprocess_for_tts(task.get("title", ""))
            priority = task.get("priority", "LOW")
            if priority == "HIGH":
                line = f"Срочно: {title}."
            elif priority == "MED":
                line = f"{title}."
            else:
                line = f"{title}."
            await speak(line)

        if total > _MAX_TASKS:
            await speak("Остальное — в заметках.")

        # 6. Завершение
        await speak("Удачи! Работаю в фоне.")

        logger.info("speech: брифинг завершён (%d задач, trigger=%s)", total, trigger)

    async def speak_summary(self, stats: dict) -> None:
        """Вечерний итог дня."""
        if not self.tts.enabled:
            return

        parts = ["Итог дня."]

        done  = stats.get("tasks_done", 0)
        total = stats.get("tasks_total", 0)
        if total > 0:
            parts.append(f"Задач выполнено: {done} из {total}.")

        hours = stats.get("deep_work_hours", 0)
        if hours:
            parts.append(
                f"Глубокой работы: {hours:.1f} часа."
                if hours < 2 else f"Глубокой работы: {hours:.0f} часов."
            )

        commits = stats.get("commits", 0)
        if commits:
            parts.append(f"Коммитов: {commits}.")

        peak = stats.get("peak_hour", "")
        if peak:
            parts.append(f"Пик активности: {peak}.")

        for part in parts:
            await self.tts.speak(part)
        logger.info("speech: итог дня озвучен")

    async def speak_error_alert(self, error: dict) -> None:
        """Уведомление о спайке ошибок."""
        if not self.tts.enabled:
            return

        error_type = preprocess_for_tts(error.get("type", "ошибка"))
        count      = error.get("count", 0)
        file_name  = error.get("file", "")
        short_file = preprocess_for_tts(file_name.split("/")[-1]) if file_name else ""

        text = f"Внимание! {count} ошибок типа {error_type}"
        if short_file:
            text += f" в файле {short_file}"
        text += ". Рекомендую разобраться."

        logger.info("speech: озвучиваем спайк ошибок")
        await self.tts.speak(text)
