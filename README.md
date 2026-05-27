# PC Assistant

Локальный AI-ассистент для Linux-разработчика.

Наблюдает за работой в фоне и несколько раз в день, в нужный момент, коротко и по делу говорит что важно — голосом и в Obsidian. Всё остаётся на машине.

## Что умеет

**Анализ и задачи**
- Следит за git-репозиториями, JetBrains IDE, буфером обмена, процессами
- Запускает LLM 3–5 раз в день — только когда ты не работаешь активно
- Формулирует задачи простым языком без жаргона: «сохранить в git» вместо «закоммитить»
- Описание каждой задачи объясняет ЗАЧЕМ, а не ЧТО делать

**Obsidian-интеграция**
- Пишет задачи в `~/Obsidian/{project}/Daily/DD.MM.YYYY.md` — каждый проект отдельно
- Создаёт структуру документации для активного проекта:
  - `Dashboard.md` — статус, коммиты, навигация (обновляется каждый цикл)
  - `Architecture.md` — из ТЗ.md/README.md репозитория (создаётся один раз)
  - `Dev Log/` — ошибки и коммиты текущей сессии
  - `Decisions/` — для твоих архитектурных решений
- При смене фокус-проекта озвучивает накопленные фоновые задачи

**Умная синхронизация задач**
- Выполненные `[x]` задачи не теряются при повторном анализе
- Детектирует задачи которые скорее всего уже выполнены (по коммитам) → напоминает → отмечает сам через 30 мин с подписью `<!-- ✓ ассистент -->`
- Двусторонняя синхронизация: отметил в Obsidian → обновилось в базе

**Фоновый анализ**
- Тихо анализирует все проекты из `~/Development/` кроме активного
- Пишет задачи в Obsidian без голоса и уведомлений
- При переключении на проект — анонсирует что накопилось

**Голос и уведомления**
- Русский нейросетевой TTS (Supertonic)
- Одно аккуратное уведомление: максимум 2 задачи, текст обрезан, счётчик остальных

## Стек

| Задача | Инструмент |
|---|---|
| LLM | Ollama + `qwen2.5:14b` (локально) |
| Голос | Supertonic TTS (нейросеть, русский) |
| Заметки | Obsidian — прямая запись в файловую систему |
| Файловые события | `watchdog` (inotify) |
| Хранилище | SQLite |
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

## Управление

```bash
# Статус сервиса
systemctl --user status pc-assistant

# Запуск / остановка / перезапуск
systemctl --user start pc-assistant
systemctl --user stop pc-assistant
systemctl --user restart pc-assistant

# Запуск при старте системы
systemctl --user enable pc-assistant

# Переключить активный проект
python main.py --focus PcAssistent
python main.py --focus Tap2Go
python main.py --focus off        # сбросить фокус

# Ручной запуск анализа
python main.py --trigger manual

# Логи
journalctl --user -u pc-assistant -f
```

## Структура проекта

```
core/         — EventBus, Orchestrator, TriggerEngine, Notifier
collectors/   — git, JetBrains, clipboard, filesystem, process monitor
llm/          — Ollama client, context builder, prompt engine, task parser
storage/      — SQLite (db, context store, focus store)
obsidian/     — client, task syncer, vault reader, project writer, progress tracker
voice/        — Supertonic TTS pipeline, speech output
productivity/ — session tracker, focus analyzer, stats builder
errors/       — runtime watcher, static analyzer, error store
ui/           — GTK system tray
systemd/      — unit-файл и install.sh
```

## Структура Obsidian

```
~/Obsidian/
  {ProjectName}/
    Dashboard.md          ← статус + git (обновляется автоматически)
    Architecture.md       ← из ТЗ.md / README.md (создаётся один раз)
    Daily/
      DD.MM.YYYY.md       ← задачи дня
    Dev Log/
      DD.MM.YYYY.md       ← коммиты, ошибки сессии
    Decisions/
      README.md           ← ты заполняешь вручную
```

## Требования

- Linux (X11 или Wayland + GNOME)
- Python 3.11+
- RAM: 8 GB минимум (16 GB рекомендуется для `qwen2.5:14b`)
- GPU: опционально (без GPU ~3–5 мин вместо ~30 сек на генерацию)
- `wmctrl` — для управления окнами на X11 (`sudo apt install wmctrl`)
- `xdotool` — для Wayland/XWayland (`sudo apt install xdotool`)

## Документация

Полная архитектура и ТЗ — [ТЗ.md](ТЗ.md)
