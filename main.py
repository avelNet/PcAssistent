#!/usr/bin/env python3
"""
PC Assistant — точка входа.

Использование:
    python main.py                    # запуск сервиса
    python main.py --trigger manual   # однократный ручной запуск LLM
    python main.py --check            # проверка окружения (Ollama, зависимости)
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path чтобы импорты работали
sys.path.insert(0, str(Path(__file__).parent))

from core.orchestrator import Orchestrator, load_config


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level_name = log_cfg.get("level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = log_cfg.get("file")
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    # Приглушаем шумные библиотеки
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run_service(config: dict) -> None:
    """Запустить как долгоживущий сервис."""
    orchestrator = Orchestrator(config)
    loop = asyncio.get_running_loop()

    # Graceful shutdown по SIGTERM/SIGINT
    stop_event = asyncio.Event()

    def _signal_handler():
        logging.getLogger(__name__).info("Получен сигнал остановки...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await orchestrator.start()
    print("\n✅ PC Assistant запущен. Ctrl+C для остановки.\n")

    await stop_event.wait()
    await orchestrator.stop()


async def run_trigger(config: dict, trigger: str) -> None:
    """
    Однократный запуск: инициализируем систему, запускаем LLM, выходим.
    Удобно для тестирования без постоянного сервиса.
    """
    logger = logging.getLogger(__name__)
    orchestrator = Orchestrator(config)

    # Ждём llm.completed вместо фиксированного sleep
    done_event = asyncio.Event()
    def _on_llm_done(data):
        done_event.set()
    orchestrator.bus.on("llm.completed", _on_llm_done)

    await orchestrator.start()

    logger.info("Запускаю триггер: %s", trigger)
    if orchestrator.trigger_engine:
        await orchestrator.trigger_engine.trigger_manual()
        # Ждём завершения LLM — таймаут 10 минут (CPU-only может быть медленным)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=600)
        except asyncio.TimeoutError:
            logger.error("Таймаут: LLM не ответила за 10 минут")

    await orchestrator.stop()


async def run_check(config: dict) -> None:
    """Проверить окружение: LLM провайдер, зависимости, git."""
    from llm.client_factory import create_llm_client
    import importlib

    print("\n🔍 Проверка окружения PC Assistant\n")
    all_ok = True

    # Python версия
    py = sys.version_info
    print(f"Python: {py.major}.{py.minor}.{py.micro}", "✓" if py >= (3, 11) else "⚠ (рекомендуется 3.11+)")

    # Зависимости
    deps = ["aiohttp", "watchdog", "psutil", "yaml"]
    for dep in deps:
        try:
            importlib.import_module(dep)
            print(f"  {dep}: ✓")
        except ImportError:
            print(f"  {dep}: ✗ — pip install {dep}")
            all_ok = False

    # LLM провайдер
    provider = config.get("llm", {}).get("provider", "ollama")
    print(f"\nLLM провайдер: {provider}")
    try:
        llm = create_llm_client(config)
        available = await llm.check_availability()
        model = getattr(llm, "model", "?")
        if available:
            print(f"  {model}: ✓")
        else:
            print(f"  ✗ {model} недоступна")
            if provider == "ollama":
                print(f"  Запусти: ollama serve && ollama pull {model}")
            elif provider == "openrouter":
                print(f"  Проверь openrouter.api_key в config.yaml")
            all_ok = False
        await llm.close()
    except Exception as e:
        print(f"  ✗ Ошибка инициализации LLM: {e}")
        all_ok = False

    # Директория БД
    db_path = Path(config.get("storage", {}).get("db_path", "~/.local/share/pc-assistant/db.sqlite")).expanduser()
    print(f"\nБД: {db_path}")
    if db_path.parent.exists():
        print("  директория: ✓")
    else:
        print("  директория будет создана при запуске")

    # Git репозитории
    print("\nGit репозитории:")
    from collectors.git_watcher import GitWatcher
    watcher = GitWatcher(config, None)
    repos = await asyncio.to_thread(watcher._scan_repos)
    if repos:
        print(f"  найдено {len(repos)} репозиториев:")
        for r in repos[:5]:
            print(f"    {r}")
        if len(repos) > 5:
            print(f"    ...и ещё {len(repos) - 5}")
    else:
        print("  ⚠ репозитории не найдены — проверь collectors.git.scan_dirs в config.yaml")

    # Текущий фокус
    from storage.focus_store import get_focus
    focus = get_focus()
    print(f"\nФокус: {'🎯 ' + focus if focus else 'все проекты'}")

    print()
    if all_ok:
        print("✅ Всё готово к запуску!")
    else:
        print("❌ Есть проблемы — устрани их перед запуском")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="PC Assistant — локальный AI ассистент")
    parser.add_argument("--config", default="config.yaml", help="Путь к конфигу")
    parser.add_argument("--trigger", help="Однократный запуск триггера (manual, morning_briefing, ...)")
    parser.add_argument("--check", action="store_true", help="Проверить окружение")
    parser.add_argument("--debug", action="store_true", help="DEBUG логирование")
    parser.add_argument(
        "--focus",
        metavar="PROJECT",
        help="Установить фокус на проект (или 'off' чтобы снять). Пример: --focus PcAssistent",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Ошибка: {e}")
        sys.exit(1)

    if args.debug:
        config.setdefault("logging", {})["level"] = "DEBUG"

    setup_logging(config)
    logger = logging.getLogger(__name__)
    logger.info("PC Assistant стартует...")

    try:
        if args.focus is not None:
            from storage.focus_store import set_focus, get_focus
            set_focus(args.focus)
            current = get_focus()
            if current:
                print(f"🎯 Фокус установлен: {current}")
                print("   LLM будет генерировать задачи только по этому проекту.")
                print("   Снять: python3 main.py --focus off")
            else:
                print("✅ Фокус снят — все проекты активны")
        elif args.check:
            asyncio.run(run_check(config))
        elif args.trigger:
            asyncio.run(run_trigger(config, args.trigger))
        else:
            asyncio.run(run_service(config))
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
