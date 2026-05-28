"""
console_chat.py — мини-чат в консоли для взаимодействия с ассистентом.

Запускается параллельно с сервисом когда stdin — терминал (не pipe/systemd).
Позволяет писать сообщения прямо в консоль не перезапуская систему.

Команды:
  /focus <проект>   — фокус на проект
  /focus off        — снять фокус
  /status           — текущее состояние
  /run              — запустить LLM прямо сейчас
  /help             — список команд
  Ctrl+C / /quit    — выход

Свободный текст — отправляется как контекст в следующий LLM-запрос
(или запускает немедленный запрос если текст — вопрос).
"""

import asyncio
import logging
import sys
import time

logger = logging.getLogger(__name__)


class ConsoleChat:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._extra_context: str = ""   # накопленный контекст от пользователя
        self._last_run_ts: float = 0

    def _bus(self):
        return self.orchestrator.bus

    async def run(self) -> None:
        """Основной цикл чтения stdin. Блокирует пока не получит EOF или /quit."""
        # В интерактивном режиме убираем логи из консоли — они мешают чату.
        # Логи остаются в файле (~/.local/share/pc-assistant/assistant.log).
        self._suppress_console_logs()

        print("\n💬 Чат активен. /help — команды, Ctrl+D — выход\n")

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            try:
                sys.stdout.write("> ")
                sys.stdout.flush()
                line_bytes = await reader.readline()
            except asyncio.CancelledError:
                break

            if not line_bytes:  # EOF (Ctrl+D)
                print("\n👋 Чат закрыт, сервис продолжает работу в фоне")
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            await self._handle(line)

    def _suppress_console_logs(self) -> None:
        """
        Убрать StreamHandler из корневого логгера в интерактивном режиме.
        Логи продолжают писаться в файл — просто не засоряют чат.
        """
        root = logging.getLogger()
        to_remove = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                     and not isinstance(h, logging.FileHandler)]
        for h in to_remove:
            root.removeHandler(h)
            logger.debug("ConsoleChat: убрал StreamHandler из логгера")

    async def _handle(self, text: str) -> None:
        """Обработать введённую строку."""
        if text.startswith("/"):
            await self._command(text)
        else:
            await self._natural_message(text)

    async def _command(self, text: str) -> None:
        """Обработать /команду."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            print("👋 Для остановки сервиса нажми Ctrl+C")

        elif cmd == "/help":
            print(
                "\nКоманды:\n"
                "  /focus <проект>  — работать только с этим проектом\n"
                "  /focus off       — снять фокус, все проекты\n"
                "  /status          — что сейчас активно\n"
                "  /run             — запустить анализ прямо сейчас\n"
                "  /clear           — сбросить накопленный контекст\n"
                "  /help            — эта справка\n"
                "\nИли просто пиши текст — он добавится как контекст к следующему запросу.\n"
                "Если текст заканчивается на '?' — запрос к LLM запустится сразу.\n"
            )

        elif cmd == "/focus":
            from storage.focus_store import set_focus, get_focus
            set_focus(arg or "off")
            focus = get_focus()
            if focus:
                print(f"🎯 Фокус: {focus}")
            else:
                print("✅ Фокус снят — все проекты активны")

        elif cmd == "/status":
            await self._show_status()

        elif cmd == "/run":
            print("🔄 Запускаю анализ...")
            await self._bus().emit("trigger.llm", {
                "reason": "manual",
                "extra_context": self._extra_context,
            })
            self._extra_context = ""

        elif cmd == "/clear":
            self._extra_context = ""
            print("🗑 Накопленный контекст сброшен")

        else:
            print(f"❓ Неизвестная команда: {cmd}. /help — список команд")

    async def _natural_message(self, text: str) -> None:
        """Обработать свободный текст."""
        from storage.focus_store import set_focus

        text_lower = text.lower()

        # Стоп-слова которые могут стоять между фразой и названием проекта
        _FILLER = {"только", "сейчас", "именно", "пока", "вот", "над", "на",
                   "по", "с", "в", "и", "а", "но"}

        # "работаем над X" / "фокус на X" → установить фокус
        for phrase in ["работаем над", "фокус на", "работаю над",
                       "сейчас делаю", "работаем только над", "занимаемся"]:
            if phrase in text_lower:
                idx = text_lower.find(phrase) + len(phrase)
                tail = text[idx:].strip().strip(",.!")
                # Пропускаем стоп-слова и берём первое значимое слово
                words = tail.split()
                project = next((w for w in words if w.lower() not in _FILLER), "")
                if project:
                    set_focus(project)
                    print(f"🎯 Понял, фокус → {project}")
                    return

        # "не трогаем X" / "игнорируй X" → добавить в контекст
        for phrase in ["не трогаем", "игнорируй", "не учитывай"]:
            if phrase in text_lower:
                self._extra_context += f"\nПользователь: {text}"
                print("📝 Учту в следующем запросе")
                return

        # Вопрос (заканчивается на ?) → немедленный запуск LLM
        if text.endswith("?"):
            self._extra_context += f"\nВопрос от пользователя: {text}"
            print("🔄 Запрашиваю...")
            await self._bus().emit("trigger.llm", {
                "reason": "user_question",
                "extra_context": self._extra_context,
            })
            self._extra_context = ""
            return

        # Всё остальное — накапливаем как контекст
        self._extra_context += f"\nКонтекст от пользователя: {text}"
        print("📝 Добавлено в контекст. /run — запустить анализ, /clear — сбросить")

    async def _show_status(self) -> None:
        """Показать текущее состояние системы."""
        from storage.focus_store import get_focus
        from storage import context_store

        focus = get_focus()
        tasks = await context_store.get_tasks_for_date()
        done = sum(1 for t in tasks if t.get("done"))
        total = len(tasks)

        engine = self.orchestrator.trigger_engine
        last_run = getattr(engine, "_last_run_ts", 0)
        if last_run:
            ago = int(time.time() - last_run)
            if ago < 60:
                last_str = f"{ago}с назад"
            elif ago < 3600:
                last_str = f"{ago // 60}мин назад"
            else:
                last_str = f"{ago // 3600}ч назад"
        else:
            last_str = "не запускался"

        llm_model = "—"
        if self.orchestrator.llm_client:
            llm_model = getattr(self.orchestrator.llm_client, "model", "—")

        print(
            f"\n📊 Статус:\n"
            f"  Фокус:          {'🎯 ' + focus if focus else 'все проекты'}\n"
            f"  Задач сегодня:  {done}/{total} выполнено\n"
            f"  Последний анализ: {last_str}\n"
            f"  Модель:         {llm_model}\n"
            f"  Накоплен контекст: {'да (' + str(len(self._extra_context)) + ' символов)' if self._extra_context else 'нет'}\n"
        )
