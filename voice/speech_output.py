"""
voice/speech_output.py — высокоуровневый интерфейс озвучки.

Форматирует задачи/статистику в короткие русские фразы и передаёт в TTSEngine.
Ограничения: максимум 5 задач, 300 символов пролога (дальше слушать неудобно).
"""

import logging

from voice.tts_engine import TTSEngine
from voice.tts_preprocessor import preprocess_for_tts

logger = logging.getLogger(__name__)

_MAX_TASKS    = 3   # больше 3 задач слушать бессмысленно — не запомнить
_MAX_PROLOGUE = 300

_PRIORITY_WORD = {"HIGH": "срочно", "MED": "важно", "LOW": ""}

_TRIGGER_GREETING = {
    "morning_briefing":    "Доброе утро! Задачи на сегодня.",
    "after_work_session":  "Сессия завершена. Вот что дальше.",
    "user_returned":       "С возвращением! Напоминаю контекст.",
    "evening_summary":     "Итог дня.",
    "manual":              "Готово.",
}


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
        Утренний/послесессионный брифинг: приветствие + пролог + топ-5 задач.
        Голос — только если voice.enabled = true.
        """
        if not self.tts.enabled or not tasks:
            return

        parts: list[str] = []

        # Приветствие по триггеру
        greeting = _TRIGGER_GREETING.get(trigger, "")
        if greeting:
            parts.append(greeting)

        # Пролог — максимум 300 символов, конвертируем английские слова
        if prologue:
            short = prologue.strip()[:_MAX_PROLOGUE]
            if len(prologue.strip()) > _MAX_PROLOGUE:
                short += "..."
            parts.append(preprocess_for_tts(short))

        # Задачи — сортируем HIGH→MED→LOW, озвучиваем топ-3 (больше не запомнить)
        sorted_tasks = sorted(
            tasks,
            key=lambda t: {"HIGH": 0, "MED": 1, "LOW": 2}.get(t.get("priority", "LOW"), 3),
        )[:_MAX_TASKS]

        total = len(tasks)
        shown = len(sorted_tasks)

        if total == 1:
            parts.append("Одна задача.")
        elif shown < total:
            parts.append("Главные задачи:")
        else:
            parts.append("Задачи:")

        for task in sorted_tasks:
            title    = task.get("title", "")
            priority = task.get("priority", "LOW")
            prefix   = _PRIORITY_WORD.get(priority, "")
            line     = f"{prefix + ' — ' if prefix else ''}{title}."
            parts.append(line)

        if total > _MAX_TASKS:
            parts.append("Остальное — в заметках.")

        text = preprocess_for_tts(" ".join(parts))
        logger.info("speech: озвучиваем брифинг (%d задач, trigger=%s)", shown, trigger)
        await self.tts.speak(text)

    async def speak_summary(self, stats: dict) -> None:
        """
        Вечерний итог дня — краткая сводка за ~30 секунд.
        stats: {deep_work_hours, commits, tasks_done, tasks_total, peak_hour}
        """
        if not self.tts.enabled:
            return

        parts = ["Итог дня."]

        done  = stats.get("tasks_done", 0)
        total = stats.get("tasks_total", 0)
        if total > 0:
            parts.append(f"Задач выполнено: {done} из {total}.")

        hours = stats.get("deep_work_hours", 0)
        if hours:
            parts.append(f"Глубокой работы: {hours:.1f} часа." if hours < 2
                         else f"Глубокой работы: {hours:.0f} часов.")

        commits = stats.get("commits", 0)
        if commits:
            parts.append(f"Коммитов: {commits}.")

        peak = stats.get("peak_hour", "")
        if peak:
            parts.append(f"Пик активности: {peak}.")

        text = " ".join(parts)
        logger.info("speech: озвучиваем итог дня")
        await self.tts.speak(text)

    async def speak_error_alert(self, error: dict) -> None:
        """Уведомление о спайке ошибок (опционально, не блокирует работу)."""
        if not self.tts.enabled:
            return

        error_type = error.get("type", "ошибка")
        count      = error.get("count", 0)
        file_name  = error.get("file", "")
        short_file = file_name.split("/")[-1] if file_name else ""

        text = f"Внимание! {count} ошибок типа {error_type}"
        if short_file:
            text += f" в файле {short_file}"
        text += ". Рекомендую разобраться."

        logger.info("speech: озвучиваем спайк ошибок")
        await self.tts.speak(text)
