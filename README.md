# PC Assistant

Фоновый AI-ассистент для разработчика на Linux. Наблюдает за работой и несколько раз в день голосом и в Obsidian говорит что важно — в нужный момент, коротко и по делу.

[![v1.1.1](https://img.shields.io/badge/version-1.1.1-blue)](https://github.com/avelNet/PcAssistent/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-green)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

---

## Что делает

- **Следит** за git-репозиториями, JetBrains IDE, буфером обмена, процессами
- **Анализирует** контекст через LLM 3–5 раз в день — только в паузах между работой
- **Говорит** голосом что важно: при переключении проекта, утром, вечером, после сессии
- **Пишет задачи** в Obsidian простым языком с объяснением ЗАЧЕМ, не ЧТО
- **Следит за зависшими** задачами: 3+ дня без закрытия → понижает приоритет

---

## Возможности

### Голос и уведомления
- Облачный русский TTS — **SberSaluteSpeech** (бесплатно 200к симв/мес)
- При переключении проекта в JetBrains: мгновенный голосовой анонс + свежие задачи
- Кликабельные уведомления GNOME → открывают Obsidian на нужной заметке
- Утренний брифинг, вечерний итог, напоминание после возвращения
- **Telegram**: дублирует срочные задачи когда отошёл от ПК (опционально)

### Задачи и Obsidian
- Задачи пишутся в `~/Obsidian/{project}/Daily/DD.MM.YYYY.md` — каждый проект отдельно
- Структура документации для каждого проекта: Dashboard, Architecture, Dev Log, Decisions, Roadmap
- Выполненные `[x]` задачи сохраняются при каждом обновлении
- Задачи предлагает закоммитить в конце рабочей сессии (LLM генерирует commit message)
- Rollover: незакрытые задачи переносятся на следующий день

### JetBrains IDE
- Определяет активный проект через `recentProjects.xml` (inotify, ≤5 сек)
- При смене проекта — голосовой анонс + пересчёт задач
- При смене git-ветки — голосовой анонс + пересчёт контекста

### LLM
- Цепочка провайдеров: **OpenRouter** (частые запросы) → **Groq** (сложные задачи) → **Ollama** (офлайн)
- Контекст фильтруется строго по активному проекту — нет смешения задач между проектами
- История команд терминала (`~/.bash_history`) в контексте LLM

---

## Стек

| Задача | Инструмент |
|--------|-----------|
| LLM (облако) | OpenRouter (бесплатные модели) + Groq (Llama 3.3 70B) |
| LLM (офлайн) | Ollama + qwen2.5:7b |
| Голос | SberSaluteSpeech REST API |
| Заметки | Obsidian — прямая запись в файловую систему |
| Файловые события | `watchdog` (inotify/kqueue) |
| Хранилище | SQLite |
| Трей | GTK AppIndicator3 (опционально) |

---

## Установка

```bash
git clone https://github.com/avelNet/PcAssistent.git
cd PcAssistent

# Проверить что будет сделано
bash install.sh --dry-run

# Установить
bash install.sh
```

Установщик интерактивно спросит:
- API-ключи (OpenRouter, Groq, SaluteSpeech)
- Путь к папке Obsidian vault
- Папку с проектами (`~/Development` по умолчанию)
- Модель Ollama для офлайн-режима (3b/7b/14b)

### API-ключи (бесплатно)

| Сервис | Где взять | Лимит |
|--------|-----------|-------|
| **OpenRouter** | [openrouter.ai/keys](https://openrouter.ai/keys) | Бесплатные модели |
| **Groq** | [console.groq.com/keys](https://console.groq.com/keys) | 1000 запросов/день |
| **SaluteSpeech** | [developers.sber.ru](https://developers.sber.ru) | 200 000 симв/мес |

---

## Конфигурация

Основной конфиг — `config.yaml`. Секреты — `config.local.yaml` (в `.gitignore`):

```yaml
# config.local.yaml
openrouter:
  api_key: "sk-or-..."

groq:
  api_key: "gsk_..."

salute_speech:
  credentials: "base64..."
  scope: "SALUTE_SPEECH_PERS"

voice:
  engine: "salute"
  voice: "Nec_24000"   # Nec=женский, Bys/Tur/Ost=мужской

# Опционально:
telegram:
  enabled: true
  bot_token: "123456:ABC..."
  chat_id: "123456789"

auto_commit:
  enabled: true

proxy:
  url: "http://user:pass@host:8080"   # HTTP или socks5://
```

---

## Управление

```bash
# Статус
systemctl --user status pc-assistant

# Перезапуск
systemctl --user restart pc-assistant

# Логи в реальном времени
journalctl --user -u pc-assistant -f

# Переключить активный проект
python main.py --focus PcAssistent
python main.py --focus off

# Ручной запуск анализа
python main.py --trigger manual
```

---

## Структура проекта

```
core/         — EventBus, Orchestrator, TriggerEngine, Notifier, Telegram
collectors/   — git, JetBrains (inotify), clipboard, filesystem, process monitor
llm/          — OpenRouter/Groq/Ollama clients, context builder, prompt engine
storage/      — SQLite (db, context store, focus store)
obsidian/     — task syncer, vault reader, project writer, progress tracker
voice/        — SaluteSpeech TTS, speech output, TTS preprocessor
productivity/ — session tracker, focus analyzer, stats builder
errors/       — runtime watcher, static analyzer, error store
ui/           — GTK system tray
systemd/      — unit-файл
install.sh    — установщик
```

---

## Структура Obsidian

```
~/Obsidian/
  {ProjectName}/
    Dashboard.md          ← статус + git (обновляется автоматически)
    Architecture.md       ← из ТЗ.md / README.md (создаётся один раз)
    Roadmap.md            ← план проекта (LLM генерирует из README)
    Daily/
      DD.MM.YYYY.md       ← задачи дня, rollover незакрытых
    Dev Log/
      DD.MM.YYYY.md       ← коммиты, ошибки сессии
    Decisions/
      README.md           ← архитектурные решения (заполняешь сам)
```

---

## Системные требования

- **OS**: Linux (Ubuntu 22.04+), GNOME Wayland или X11
- **Python**: 3.11+
- **Пакеты**: `aplay` (alsa-utils), `notify-send` ≥0.8 (libnotify-bin), `wl-clipboard`
- **RAM**: 4 GB минимум (без Ollama); 8 GB для qwen2.5:7b офлайн
- **Интернет**: нужен для OpenRouter/Groq/SaluteSpeech; Ollama работает офлайн
