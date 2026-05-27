# PC Assistant

Локальный AI-ассистент для Linux-разработчика.

Наблюдает за твоей работой в фоне и несколько раз в день, в нужный момент, коротко и по делу говорит что важно — голосом или в Obsidian.

## Как это работает

- Следит за git-репозиториями, JetBrains IDE, файловой системой
- Замечает паттерны ошибок в PyCharm, отслеживает продуктивность
- Запускает локальный LLM (Ollama) 3–5 раз в день — не чаще
- Пишет задачи в Obsidian Daily Note, озвучивает голосом (Piper TTS)
- Всё остаётся на машине. Интернет не нужен

## Стек

| Задача | Инструмент |
|---|---|
| LLM | Ollama + `qwen2.5:14b` |
| Файловые события | `watchdog` (inotify) |
| Голос | Piper TTS (`ru_RU-ruslan-medium`) |
| Хранилище | SQLite |
| Obsidian | Local REST API плагин |
| Трей | GTK AppIndicator3 |

## Быстрый старт

```bash
# Зависимости
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5:14b   # или qwen2.5:7b для слабых машин

# Проверка
python main.py --check

# Первый запуск
python main.py --trigger manual
```

## Структура

```
core/        — EventBus, Orchestrator, TriggerEngine
collectors/  — git, JetBrains, fs, clipboard, process, focus
llm/         — Ollama client, context builder, prompt engine, task parser
storage/     — SQLite (db + context store)
obsidian/    — REST API клиент, watcher, task syncer
voice/       — Piper TTS pipeline
productivity/ — session tracker, focus analyzer, stats
errors/      — runtime watcher, static analyzer, error store
ui/          — GTK system tray
```

## Требования

- Linux (X11 или Wayland)
- Python 3.11+
- RAM: 8 GB минимум (16 GB для `qwen2.5:14b`)
- GPU: опционально (без GPU генерация ~3–5 мин вместо ~30 сек)

## Документация

Полная архитектура и ТЗ — [ТЗ.md](ТЗ.md)
