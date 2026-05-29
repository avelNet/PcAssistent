"""
trigger_engine.py — единственный модуль который решает запускать ли LLM.
Все остальные модули только эмитят события — решение всегда здесь.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, date

from core.event_bus import EventBus
from llm.context_builder import ContextBuilder
from llm import prompt_engine, task_parser
from obsidian.project_writer import ProjectWriter
from storage import context_store

logger = logging.getLogger(__name__)


class TriggerEngine:
    def __init__(self, config: dict, bus: EventBus,
                 llm, context_builder: ContextBuilder):
        self.config = config.get("trigger", {})
        self._full_config = config  # нужен для SpeechOutput
        self.bus = bus
        self.llm = llm
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

        # Отслеживаем переключения фокуса для анонса фоновых задач.
        # Инициализируем из focus.json сразу — иначе JetBrainsWatcher может
        # успеть сменить focus до того как мы запомним старое значение.
        from storage.focus_store import get_focus as _get_focus
        self._last_known_focus: str | None = _get_focus()

        # Документация проекта в Obsidian
        self._project_writer = ProjectWriter(config)

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
        self.bus.on("jetbrains.changed", self._on_jetbrains_changed)
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

    async def _on_jetbrains_changed(self, data: dict) -> None:
        """
        IDE сменила активный проект → автоматически переключаем фокус
        и запускаем LLM-цикл с новым проектом (если auto_focus_from_ide=true).
        Работает для любых JetBrains-проектов, не только git-репозиториев в
        ~/Development/ — поддерживает приватные репо, чужие папки и т.д.
        """
        if not data:
            return

        auto_cfg = self._full_config.get("auto_focus_from_ide", True)
        if not auto_cfg:
            return

        new_active = data.get("active_project")
        if not new_active:
            return

        from storage.focus_store import get_focus, set_focus
        current = get_focus()
        if current == new_active:
            return  # уже активен

        logger.info(
            "TriggerEngine: IDE сменил активный проект %s → %s, переключаю фокус",
            current or "нет", new_active,
        )
        set_focus(new_active)
        self._last_known_focus = new_active  # сразу обновляем — _run_llm не будет дублировать

        # Анонс немедленно — читает готовый Obsidian-файл, LLM не нужен
        branch = None
        snap = await self.context_builder.get_project_snapshot(new_active)
        if snap:
            branch = snap.get("branch")
        asyncio.create_task(
            self._project_writer.announce_focus_switch(
                new_active, self._full_config, branch=branch
            ),
            name="focus_switch_announce",
        )

        # LLM-анализ — генерирует свежие задачи, пишет в Obsidian
        await self._try_run("focus_switch", voice=False, force=True)

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

        # Утренний брифинг — один раз в день, только до вечернего часа
        # (если сервис стартовал вечером — morning уже прошло, не показываем)
        if (morning_hour <= hour < evening_hour
                and self._morning_done_date != today
                and not self._is_locked):
            self._morning_done_date = today
            logger.info("TriggerEngine: утренний брифинг (%d:xx, запланирован на %d:xx)",
                        hour, morning_hour)
            from storage.focus_store import get_focus
            focus = get_focus()
            if focus:
                await self.bus.emit("trigger.rollover", {"project": focus})
            await self._try_run("morning_briefing", voice=True)

        # Вечерний итог — один раз в день начиная с evening_hour
        elif (hour >= evening_hour
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

            current_focus = context.get("focus")

            # 2. Проверяем что контекст изменился
            context_hash = hashlib.md5(
                json.dumps(context, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()

            if context_hash == self._last_context_hash and not extra:
                logger.info("TriggerEngine: контекст не изменился, пропускаем")
                return

            # 3. Проверяем доступность LLM
            if not await self.llm.check_availability():
                provider = self._full_config.get("llm", {}).get("provider", "ollama")
                logger.error("TriggerEngine: LLM недоступна! провайдер=%s", provider)
                from core.notifier import notify_error
                await notify_error(f"LLM недоступна ({provider}). Проверь ключ или соединение.")
                return

            # 4. Формируем промпт
            system_prompt, user_prompt = prompt_engine.build(trigger, context)

            # 5. Запрашиваем LLM
            raw_text, meta = await self.llm.complete(system_prompt, user_prompt)

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

            # 7. Заменяем задачи за сегодня (не накапливаем — каждый запуск свежий список)
            if tasks:
                await context_store.replace_today_tasks(tasks)

            # 8. Сохраняем статистику запуска
            duration = time.monotonic() - start
            await context_store.save_llm_run(
                trigger=trigger,
                model=meta.get("model", self.llm.model),
                duration_s=duration,
                tokens=meta.get("tokens_total", 0),
                task_count=len(tasks),
            )

            # 9. Обновляем состояние
            self._last_run_ts = time.time()
            self._last_context_hash = context_hash
            self._last_known_focus = current_focus  # запоминаем фокус ПОСЛЕ успешного запуска

            # 10. Уведомляем систему → TaskSyncer запишет в Obsidian
            await self.bus.emit("llm.completed", {
                "tasks": tasks,
                "prologue": prologue,
                "trigger": trigger,
                "voice": voice,
                "meta": meta,
            })
            # Даём Obsidian-запись завершиться до старта TTS
            await asyncio.sleep(0.5)

            # 11. Голос — стартуем в фоне, не блокируем
            from voice.speech_output import SpeechOutput
            if voice:
                sp = SpeechOutput(self._full_config)
                asyncio.create_task(
                    sp.speak_tasks(tasks, prologue=prologue, trigger=trigger),
                    name="speak_tasks",
                )

            # 12. Уведомление — кликабельное (XDG portal с кнопкой "Открыть Obsidian")
            # focus_switch пропускаем: announce_focus_switch уже отправил своё уведомление
            # (два portal-уведомления с одним notif_id заменяют друг друга в GNOME)
            if trigger != "focus_switch":
                from core.notifier import notify_tasks

                # Путь к Daily файлу активного проекта для клика → Obsidian
                obsidian_path = None
                if current_focus and self._project_writer.shared_root:
                    from datetime import date as _date
                    daily_folder = self._full_config.get("obsidian", {}).get("daily_folder", "Daily")
                    obsidian_path = str(
                        self._project_writer.shared_root / current_focus
                        / daily_folder / f"{_date.today().strftime('%d.%m.%Y')}.md"
                    )

                async def _delayed_notify():
                    if voice:
                        await asyncio.sleep(3)
                    await notify_tasks(
                        tasks, prologue=prologue, trigger=trigger,
                        obsidian_path=obsidian_path,
                    )

                asyncio.create_task(_delayed_notify(), name="notify_tasks")

            # 13. Детектируем возможно-выполненные задачи и планируем авто-завершение
            possibly_done = self._find_possibly_done_tasks(tasks, context)
            for pd_task in possibly_done:
                asyncio.create_task(
                    self._schedule_auto_complete(pd_task, current_focus, delay_min=30),
                    name=f"auto_complete_{pd_task.get('id', '')[:8]}",
                )

            # 14. Логируем результат
            logger.info("TriggerEngine: LLM завершён за %.1fс, задач=%d", duration, len(tasks))
            logger.info("\n%s", task_parser.format_tasks_for_display(tasks))
            logger.info("═" * 50)

            # 14. Документация активного проекта — обновляем структуру в Obsidian
            if current_focus and self._project_writer.enabled:
                git_snap  = await self.context_builder.get_project_snapshot(current_focus)
                repo_path = await self.context_builder.get_project_git_path(current_focus)
                errors    = context.get("errors")

                # Генерируем Roadmap только если файл ещё не существует
                roadmap_content: str | None = None
                if not self._project_writer.roadmap_exists(current_focus):
                    roadmap_content = await self._generate_roadmap(
                        current_focus, repo_path
                    )

                asyncio.create_task(
                    self._project_writer.write_project_structure(
                        project=current_focus,
                        repo_path=repo_path,
                        git_snapshot=git_snap,
                        errors=errors,
                        roadmap_content=roadmap_content,
                    ),
                    name="project_structure_update",
                )

            # 15. Запускаем фоновый анализ остальных проектов (не блокирует)
            active_project = current_focus
            asyncio.create_task(
                self._run_background_projects(skip=active_project),
                name="background_analysis",
            )

            # 16. Сигнал: весь цикл завершён (TTS сыгран, уведомления отправлены)
            #     Используется run_trigger чтобы не выходить раньше времени
            await self.bus.emit("llm.all_done", {"trigger": trigger})

        except Exception as e:
            # Ловим OllamaError, OpenRouterError и любые другие ошибки LLM
            logger.error("TriggerEngine: ошибка LLM — %s", e)
            if logger.isEnabledFor(logging.DEBUG):
                logger.exception("TriggerEngine: детали ошибки")
        finally:
            self._running = False

    # ─── Фоновый анализ других проектов ─────────────────────────────────────

    async def _run_background_projects(self, skip: str | None = None) -> None:
        """
        Тихий анализ всех проектов из ~/Development/ кроме активного.
        Запускается после основного LLM-цикла, не блокирует UI.
        """
        # Ждём немного — TTS ещё может играть, не перегружаем LLM сразу
        await asyncio.sleep(10)

        try:
            all_projects = await self.context_builder.get_known_projects()
        except Exception as e:
            logger.debug("background: не удалось получить проекты: %s", e)
            return

        # Исключаем активный проект и дубликаты
        projects = list(dict.fromkeys(
            p for p in all_projects if p and p != skip
        ))

        if not projects:
            return

        logger.info("TriggerEngine[bg]: фоновый анализ %d проектов: %s",
                    len(projects), ", ".join(projects))

        for project in projects:
            # Пауза между проектами — не атакуем API залпом
            await asyncio.sleep(5)
            await self._run_background_single(project)

    async def _run_background_single(self, project: str) -> None:
        """Один фоновый цикл для конкретного проекта: без голоса, без уведомлений."""
        try:
            logger.debug("TriggerEngine[bg]: анализ '%s'...", project)

            context = await self.context_builder.build(
                "manual", project_override=project
            )
            system_prompt, user_prompt = prompt_engine.build("manual", context)
            raw_text, meta = await self.llm.complete(system_prompt, user_prompt)

            result = task_parser.parse(raw_text)
            tasks  = result["tasks"]
            if not tasks:
                logger.debug("TriggerEngine[bg]: '%s' — задач нет", project)
                return

            # Сохраняем только задачи этого проекта (не трогаем фокусные)
            await context_store.replace_today_tasks(tasks, project=project)

            # Уведомляем TaskSyncer → тихая запись в Obsidian
            await self.bus.emit("llm.background_completed", {
                "tasks":   tasks,
                "prologue": result.get("prologue", ""),
                "project": project,
            })

            logger.info("TriggerEngine[bg]: '%s' — %d задач записано", project, len(tasks))

            # Создаём структуру документации для фонового проекта
            if self._project_writer.enabled:
                git_snap  = await self.context_builder.get_project_snapshot(project)
                repo_path = await self.context_builder.get_project_git_path(project)
                errors    = context.get("errors")

                roadmap_content: str | None = None
                if not self._project_writer.roadmap_exists(project):
                    roadmap_content = await self._generate_roadmap(project, repo_path)

                asyncio.create_task(
                    self._project_writer.write_project_structure(
                        project=project,
                        repo_path=repo_path,
                        git_snapshot=git_snap,
                        errors=errors,
                        roadmap_content=roadmap_content,
                    ),
                    name=f"bg_structure_{project}",
                )

        except Exception as e:
            logger.debug("TriggerEngine[bg]: '%s' ошибка: %s", project, e)

    # ─── Авто-завершение задач ───────────────────────────────────────────────

    def _find_possibly_done_tasks(
        self, tasks: list[dict], context: dict
    ) -> list[dict]:
        """
        Найти задачи которые скорее всего уже выполнены.
        Критерий: 2+ значимых слова из заголовка задачи встречаются в
        последних git-коммитах.

        Возвращает список незавершённых задач-кандидатов.
        """
        # Собираем все строки коммитов из git-снапшотов
        commit_lines: list[str] = []
        for repo in context.get("git", []):
            log = repo.get("recent_log", "")
            if log:
                commit_lines.extend(log.lower().splitlines())

        if not commit_lines:
            return []

        # Стоп-слова которые не считаем значимыми
        _STOPWORDS = {
            "что", "для", "это", "при", "как", "все", "или", "если", "ещё",
            "уже", "из", "по", "на", "не", "в", "и", "с", "то", "же",
            "the", "and", "for", "with", "from", "that", "this", "are", "was",
            "feat", "fix", "docs", "chore", "refactor", "add", "update", "remove",
        }

        possibly_done = []
        for task in tasks:
            if task.get("done"):
                continue

            title = task.get("title", "").lower()
            # Значимые токены: слова длиннее 3 символов, не стоп-слова
            tokens = {
                w for w in re.findall(r'[a-zа-яё]{4,}', title)
                if w not in _STOPWORDS
            }
            if len(tokens) < 2:
                continue

            # Сколько токенов встречается в коммитах
            matched = sum(
                1 for t in tokens
                if any(t in line for line in commit_lines)
            )
            # Порог: 2+ совпадения ИЛИ более половины токенов
            if matched >= 2 or (tokens and matched / len(tokens) >= 0.5):
                logger.debug(
                    "TriggerEngine: возможно выполнено (%d токенов) — '%s'",
                    matched, task.get("title")
                )
                possibly_done.append(task)

        return possibly_done

    async def _schedule_auto_complete(
        self,
        task: dict,
        project: str | None,
        delay_min: int = 30,
    ) -> None:
        """
        Напомнить пользователю об незакрытой задаче, затем через delay_min минут
        автоматически отметить её как выполненную (если пользователь не сделал сам).
        """
        task_id    = task.get("id", "")
        task_title = task.get("title", "")

        if not task_id:
            return

        # Сразу отправляем напоминание
        from core.notifier import notify_check_task
        await notify_check_task(task_title, delay_min=delay_min)

        # Ждём
        await asyncio.sleep(delay_min * 60)

        # Перепроверяем — пользователь мог уже отметить сам
        already_done = await self._project_writer.is_task_done_in_obsidian(
            task_id, project or ""
        )
        if already_done:
            logger.info(
                "TriggerEngine: задача '%s' уже отмечена пользователем — авто-завершение отменено",
                task_title
            )
            return

        # Автоматически помечаем
        marked = await self._project_writer.auto_complete_task_fs(
            task_id=task_id,
            project=project or "",
            done_by="ассистент",
        )

        if marked:
            # Обновляем SQLite
            from storage import context_store as cs
            await cs.update_tasks_from_obsidian([{"id": task_id, "done": True}])

            # Уведомляем пользователя
            from core.notifier import notify_auto_completed
            await notify_auto_completed(task_title)

            logger.info(
                "TriggerEngine: задача '%s' авто-завершена ассистентом", task_title
            )

    async def _generate_roadmap(
        self,
        project: str,
        repo_path: str | None,
    ) -> str | None:
        """
        Одиночный LLM-запрос для генерации стратегического Roadmap проекта.
        Читает README/ТЗ из репозитория и просит LLM предложить долгосрочные задачи.
        Возвращает markdown-текст или None при ошибке.
        """
        logger.info("TriggerEngine: генерирую Roadmap для '%s'...", project)

        # Читаем исходники проекта
        source_text = ""
        if repo_path:
            from pathlib import Path as _Path
            repo = _Path(repo_path)
            for fname in ("ТЗ.md", "TZ.md", "README.md", "readme.md"):
                src = repo / fname
                if src.exists():
                    try:
                        source_text = src.read_text(encoding="utf-8")[:6000]
                        break
                    except Exception:
                        pass

        if not source_text:
            source_text = f"Проект: {project}. Описание недоступно."

        system = (
            "Ты стратегический ассистент разработчика. "
            "Анализируй проект и предлагай конкретные долгосрочные улучшения. "
            "Отвечай только на русском языке простыми словами без жаргона. "
            "Формат ответа — строго markdown."
        )
        user = (
            f"Проект: {project}\n\n"
            f"Описание:\n{source_text}\n\n"
            "Составь Roadmap — список долгосрочных задач и улучшений для этого проекта.\n"
            "Думай стратегически: платформы, масштабируемость, UX, интеграции, надёжность.\n\n"
            "Структура ответа (строго):\n"
            "## 📅 Планируется\n"
            "<!-- 3-5 конкретных задач которые реально нужны -->\n"
            "- [ ] Задача\n\n"
            "## 💡 Идеи\n"
            "<!-- 3-5 идей для развития без конкретных сроков -->\n"
            "- [ ] Идея\n\n"
            "## ✅ Сделано\n"
            "<!-- оставь пустым -->\n\n"
            "Не добавляй ничего кроме этих трёх секций."
        )

        try:
            raw, _ = await self.llm.complete(system, user)
            # Берём только начиная с первого ##
            lines = raw.splitlines()
            start = next((i for i, ln in enumerate(lines) if ln.startswith("##")), 0)
            return "\n".join(lines[start:]).strip() + "\n"
        except Exception as e:
            logger.debug("TriggerEngine: не удалось сгенерировать Roadmap — %s", e)
            return None

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
