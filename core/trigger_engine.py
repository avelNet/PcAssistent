"""
trigger_engine.py — единственный модуль который решает запускать ли LLM.
Все остальные модули только эмитят события — решение всегда здесь.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, date

from core.event_bus import EventBus
from llm.context_builder import ContextBuilder
from llm import prompt_engine, task_parser
from storage import context_store

logger = logging.getLogger(__name__)


class TriggerEngine:
    def __init__(self, config: dict, bus: EventBus,
                 ollama, context_builder: ContextBuilder):
        self.config = config.get("trigger", {})
        self.bus = bus
        self.ollama = ollama
        self.context_builder = context_builder

        self._last_run_ts: float = 0
        self._last_context_hash: str = ""
        self._running: bool = False
        self._pending_spike: dict | None = None

        # Состояние процессов (обновляется от process_monitor)
        self._is_working: bool = False
        self._is_leisure: bool = False
        self._is_locked: bool = False
        self._idle_minutes: int = 0

        # Расписание: утро/вечер — запускаем только раз в день
        self._morning_done_date: date | None = None
        self._evening_done_date: date | None = None

        self._subscribe()
        asyncio.create_task(self._schedule_loop(), name="trigger_schedule")

    def _subscribe(self) -> None:
        """Подписаться на все события которые могут инициировать запуск LLM."""
        self.bus.on("trigger.llm", self._on_manual)
        self.bus.on("git.changed", self._on_git_changed)
        self.bus.on("errors.spike", self._on_errors_spike)
        self.bus.on("session.ended", self._on_session_ended)
        self.bus.on("user.returned", self._on_user_returned)
        self.bus.on("process.snapshot", self._on_process_snapshot)
        logger.debug("TriggerEngine: подписки установлены")

    # ─── Обработчики событий ────────────────────────────────────────────────

    async def _on_manual(self, data: dict) -> None:
        """Ручной запуск из трея, консольного чата или напрямую."""
        reason = data.get("reason", "manual") if data else "manual"
        logger.info("TriggerEngine: ручной запуск [%s]", reason)

        # Дополнительный контекст от пользователя (из консольного чата)
        extra = {}
        if data and data.get("extra_context"):
            extra["user_message"] = data["extra_context"]

        await self._try_run("manual", voice=True, force=True, extra=extra or None)

    async def _on_git_changed(self, data: dict) -> None:
        """Новый коммит — запустить через 2 минуты тишины."""
        logger.debug("TriggerEngine: git.changed — планирую отложенную проверку")
        await asyncio.sleep(120)  # 2 минуты
        await self._try_run("git_activity", voice=False)

    async def _on_errors_spike(self, data: dict) -> None:
        """Спайк ошибок — запустить при ближайшей паузе в работе."""
        self._pending_spike = data
        logger.info("TriggerEngine: спайк ошибок запомнен (%s × %d в %s)",
                    data.get("type"), data.get("count"), data.get("file"))
        await self._try_run("errors_spike", voice=False, extra={"errors_spike": data})

    async def _on_session_ended(self, data: dict) -> None:
        """Рабочая сессия закончилась."""
        duration = data.get("duration_min", 0) if data else 0
        logger.info("TriggerEngine: сессия закончена (%.0f мин)", duration)

        extra = {}
        if self._pending_spike:
            extra["errors_spike"] = self._pending_spike
            self._pending_spike = None

        await self._try_run("after_work_session", voice=False,
                            extra={"session": data, **extra} if extra else {"session": data})

    async def _on_user_returned(self, data: dict) -> None:
        """Пользователь вернулся после долгого перерыва."""
        idle_min = data.get("idle_was_min", 0) if data else 0
        logger.info("TriggerEngine: возвращение после %d мин", idle_min)
        await self._try_run("user_returned", voice=True,
                            extra={"idle_was_min": idle_min})

    def _on_process_snapshot(self, data: dict) -> None:
        """Обновить состояние процессов (синхронный обработчик)."""
        if not data:
            return
        self._is_working = data.get("is_working", False)
        self._is_leisure = data.get("is_leisure", False)
        self._is_locked = data.get("is_locked", False)
        self._idle_minutes = data.get("idle_minutes", 0)

    # ─── Расписание утро/вечер ───────────────────────────────────────────────

    async def _schedule_loop(self) -> None:
        """Цикл проверки расписания — утренний и вечерний брифинги."""
        # Ждём пока система полностью запустится
        await asyncio.sleep(30)

        while True:
            try:
                await self._check_schedule()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("TriggerEngine: ошибка в schedule_loop")
            await asyncio.sleep(60)  # проверяем каждую минуту

    async def _check_schedule(self) -> None:
        """Проверить нужно ли запустить утренний или вечерний брифинг."""
        now = datetime.now()
        today = now.date()
        hour = now.hour

        morning_hour = self.config.get("morning_hour", 9)
        evening_hour = self.config.get("evening_hour", 19)

        # Утренний брифинг — один раз в день в morning_hour
        if (hour == morning_hour
                and self._morning_done_date != today
                and not self._is_locked):
            self._morning_done_date = today
            logger.info("TriggerEngine: утренний брифинг (%d:xx)", morning_hour)
            await self._try_run("morning_briefing", voice=True)

        # Вечерний итог — один раз в день в evening_hour
        elif (hour == evening_hour
              and self._evening_done_date != today
              and not self._is_locked):
            self._evening_done_date = today
            logger.info("TriggerEngine: вечерний итог (%d:xx)", evening_hour)
            await self._try_run("evening_summary", voice=True)

    # ─── Основная логика ────────────────────────────────────────────────────

    async def _try_run(self, trigger: str, voice: bool = False,
                       force: bool = False, extra: dict | None = None) -> None:
        """Проверить блокировки и запустить LLM если всё ок."""
        reason = self._can_run(force=force)
        if reason:
            logger.info("TriggerEngine: СТОП [%s] для триггера '%s'", reason, trigger)
            return

        if self._running:
            logger.info("TriggerEngine: LLM уже работает, пропускаем '%s'", trigger)
            return

        await self._run_llm(trigger, voice=voice, extra=extra)

    def _can_run(self, force: bool = False) -> str | None:
        """
        Проверить блокировки. Возвращает причину блокировки или None если можно.
        Порядок проверок важен — от самой очевидной к менее очевидной.
        """
        if force:
            return None  # ручной запуск всегда проходит

        if self._is_locked:
            return "screen_locked"

        if self._is_leisure:
            return "leisure"

        if self._is_working and self._idle_minutes < 10:
            return "active_work"

        min_interval_h = self.config.get("min_interval_hours", 2)
        elapsed_h = (time.time() - self._last_run_ts) / 3600
        if elapsed_h < min_interval_h:
            return f"too_soon ({elapsed_h:.1f}h < {min_interval_h}h)"

        return None

    async def _run_llm(self, trigger: str, voice: bool, extra: dict | None = None) -> None:
        """Полный цикл: контекст → промпт → Ollama → задачи → события."""
        self._running = True
        start = time.monotonic()
        logger.info("═" * 50)
        logger.info("TriggerEngine: запуск LLM [триггер=%s, голос=%s]", trigger, voice)

        try:
            # 1. Собираем контекст
            context = await self.context_builder.build(trigger, extra=extra)

            # 2. Проверяем что контекст изменился
            context_hash = hashlib.md5(
                json.dumps(context, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()

            if context_hash == self._last_context_hash and not extra:
                logger.info("TriggerEngine: контекст не изменился, пропускаем")
                return

            # 3. Проверяем доступность Ollama
            if not await self.ollama.check_availability():
                logger.error(
                    "TriggerEngine: Ollama недоступна! "
                    "Запусти: ollama serve && ollama pull %s",
                    self.ollama.model
                )
                return

            # 4. Формируем промпт
            system_prompt, user_prompt = prompt_engine.build(trigger, context)

            # 5. Запрашиваем LLM
            raw_text, meta = await self.ollama.complete(system_prompt, user_prompt)

            # 6. Парсим задачи
            result = task_parser.parse(raw_text)
            tasks = result["tasks"]
            prologue = result["prologue"]

            # 6b. Пост-фильтр: убрать задачи которые совпадают с "не начато" канбана
            # но НЕ совпадают с "в работе" — защита от игнорирования канбана LLM
            kanban = context.get("obsidian", {}).get("kanban", {})
            if kanban:
                tasks = _filter_tasks_by_kanban(tasks, kanban)
                logger.info("TriggerEngine: после канбан-фильтра задач=%d", len(tasks))

            # 7. Сохраняем задачи в БД
            if tasks:
                await context_store.save_tasks(tasks)

            # 8. Сохраняем статистику запуска
            duration = time.monotonic() - start
            await context_store.save_llm_run(
                trigger=trigger,
                model=meta.get("model", self.ollama.model),
                duration_s=duration,
                tokens=meta.get("tokens_total", 0),
                task_count=len(tasks),
            )

            # 9. Обновляем состояние
            self._last_run_ts = time.time()
            self._last_context_hash = context_hash

            # 10. Desktop-уведомление с задачами
            from core.notifier import notify_tasks
            await notify_tasks(tasks, prologue=prologue, trigger=trigger)

            # 11. Уведомляем систему
            await self.bus.emit("llm.completed", {
                "tasks": tasks,
                "prologue": prologue,
                "trigger": trigger,
                "voice": voice,
                "meta": meta,
            })

            # 11. Логируем результат
            logger.info("TriggerEngine: LLM завершён за %.1fс, задач=%d", duration, len(tasks))
            logger.info("\n%s", task_parser.format_tasks_for_display(tasks))
            logger.info("═" * 50)

        except Exception as e:
            # Ловим OllamaError, OpenRouterError и любые другие ошибки LLM
            logger.error("TriggerEngine: ошибка LLM — %s", e)
            if logger.isEnabledFor(logging.DEBUG):
                logger.exception("TriggerEngine: детали ошибки")
        finally:
            self._running = False

    # ─── Публичные методы ────────────────────────────────────────────────────

    async def trigger_manual(self) -> None:
        """Ручной запуск (для трея и тестирования)."""
        await self.bus.emit("trigger.llm", {"reason": "manual", "priority": "normal"})


# ─── Вспомогательные функции ────────────────────────────────────────────────

def _tokenize(text: str) -> set[str]:
    """Разбить строку на значимые токены (числа + слова > 2 символов)."""
    import re
    return set(re.findall(r'\d+|[a-zа-яё]{3,}', text.lower()))


def _filter_tasks_by_kanban(tasks: list[dict], kanban: dict) -> list[dict]:
    """
    Убрать задачи которые совпадают с 'не начато' но НЕ совпадают с 'в работе'.

    Логика:
      - Собираем токены всех "в работе" пунктов → wip_tokens
      - Собираем токены всех "не начато" пунктов → blocked_tokens
      - Задача удаляется если: пересекается с blocked_tokens И НЕ пересекается с wip_tokens
      - Если "в работе" пусто — фильтрация не применяется
    """
    wip_tokens: set[str] = set()
    blocked_tokens: set[str] = set()

    for columns in kanban.values():
        for item in columns.get("in_progress", []):
            wip_tokens.update(_tokenize(item))
        for item in columns.get("not_started", []):
            blocked_tokens.update(_tokenize(item))

    if not wip_tokens:
        return tasks  # нечего фильтровать — канбан пустой

    # Убираем общие токены (числа, предлоги) чтобы не было ложных срабатываний
    # Числа важны: "01", "02" etc. — не убираем их
    # Убираем только слова которые есть и в wip и в blocked (т.к. они не различают)
    ambiguous = wip_tokens & blocked_tokens
    distinguishing_blocked = blocked_tokens - ambiguous

    if not distinguishing_blocked:
        return tasks

    filtered = []
    for task in tasks:
        task_tokens = _tokenize(task.get("title", ""))
        hits_blocked = bool(task_tokens & distinguishing_blocked)
        hits_wip = bool(task_tokens & wip_tokens)

        if hits_blocked and not hits_wip:
            logger.info(
                "TriggerEngine: фильтр канбана убрал задачу '%s' (совпадение с 'не начато')",
                task.get("title", "")
            )
        else:
            filtered.append(task)

    return filtered
