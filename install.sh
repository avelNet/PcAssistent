#!/usr/bin/env bash
# install.sh — установщик PC Assistant
# Использование:
#   bash install.sh            — обычная установка
#   bash install.sh --dry-run  — показать что будет сделано, ничего не менять
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="pc-assistant"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
CONFIG_LOCAL="$REPO_DIR/config.local.yaml"
VENV_DIR="$REPO_DIR/.venv"

DRY_RUN=false
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

# ─── Цвета ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}→${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}!${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}$*${NC}"; }
dry()     { echo -e "${YELLOW}[dry-run]${NC} $*"; }

if $DRY_RUN; then
    echo -e "${YELLOW}${BOLD}=== РЕЖИМ ПРОВЕРКИ (--dry-run) — ничего не изменяется ===${NC}\n"
fi

# ─── 1. Проверка Python ───────────────────────────────────────────────────────
header "Проверка зависимостей"

PYTHON=""
for py in python3.12 python3.11 python3; do
    if command -v "$py" &>/dev/null; then
        version=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=${version%%.*}; minor=${version##*.}
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$py"
            success "Python $version найден: $(command -v $py)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    error "Python 3.11+ не найден. Установи: sudo apt install python3.11"
    exit 1
fi

# ─── 2. Системные пакеты ─────────────────────────────────────────────────────
MISSING_PKGS=()
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        MISSING_PKGS+=("$2")
        warn "$1 не найден (пакет: $2)"
    else
        success "$1 найден"
    fi
}

check_cmd aplay          alsa-utils
check_cmd notify-send    libnotify-bin
check_cmd wl-paste       wl-clipboard
check_cmd git            git

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    echo
    warn "Отсутствующие пакеты: ${MISSING_PKGS[*]}"
    if $DRY_RUN; then
        dry "sudo apt install -y ${MISSING_PKGS[*]}"
    else
        read -rp "Установить автоматически через apt? [Y/n]: " install_pkgs
        if [[ "${install_pkgs:-Y}" =~ ^[Yy] ]]; then
            sudo apt install -y "${MISSING_PKGS[@]}"
        else
            warn "Продолжаем без некоторых пакетов — часть функций может не работать"
        fi
    fi
fi

# ─── 3. Виртуальное окружение ─────────────────────────────────────────────────
header "Установка зависимостей Python"

if $DRY_RUN; then
    dry "python3 -m venv $VENV_DIR"
    dry "pip install -r $REPO_DIR/requirements.txt"
    success "venv: $VENV_DIR (не создан — dry-run)"
else
    if [[ ! -d "$VENV_DIR" ]]; then
        info "Создаю виртуальное окружение..."
        "$PYTHON" -m venv "$VENV_DIR"
    fi
    success "venv: $VENV_DIR"
    info "Устанавливаю Python-пакеты..."
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" -q
    success "Зависимости установлены"
fi

# ─── 4. Конфигурация ─────────────────────────────────────────────────────────
header "Настройка конфигурации"

if $DRY_RUN; then
    if [[ -f "$CONFIG_LOCAL" ]]; then
        success "config.local.yaml уже существует — будет сохранён"
    else
        dry "Создать $CONFIG_LOCAL с API-ключами (интерактивно)"
    fi
elif [[ -f "$CONFIG_LOCAL" ]]; then
    warn "config.local.yaml уже существует — пропускаем создание"
    warn "Если нужно изменить ключи — отредактируй: $CONFIG_LOCAL"
else
    echo
    echo "Для работы нужен хотя бы один LLM-провайдер."
    echo "Оставь поле пустым чтобы пропустить."
    echo

    read -rp "OpenRouter API key (https://openrouter.ai/keys): " OPENROUTER_KEY
    read -rp "Groq API key (https://console.groq.com/keys):     " GROQ_KEY
    echo
    echo "TTS — голосовые уведомления (опционально)."
    echo "SaluteSpeech: зарегистрируйся на developers.sber.ru → создай проект → скопируй credentials."
    read -rp "SaluteSpeech credentials (base64, или Enter чтобы пропустить): " SALUTE_KEY
    echo

    # Ollama — офлайн fallback
    OLLAMA_MODEL=""
    if command -v ollama &>/dev/null; then
        echo "Ollama найден — выбери модель для офлайн-режима:"
        echo "  1) qwen2.5:3b    (~2 GB RAM) — быстрая, базовое качество"
        echo "  2) qwen2.5:7b    (~5 GB RAM) — хороший баланс (рекомендуется)"
        echo "  3) qwen2.5:14b   (~10 GB RAM) — лучшее качество"
        echo "  4) Пропустить"
        read -rp "Выбор [2]: " OLLAMA_CHOICE
        case "${OLLAMA_CHOICE:-2}" in
            1) OLLAMA_MODEL="qwen2.5:3b" ;;
            2) OLLAMA_MODEL="qwen2.5:7b" ;;
            3) OLLAMA_MODEL="qwen2.5:14b" ;;
        esac
        if [[ -n "$OLLAMA_MODEL" ]]; then
            info "Загружаю модель $OLLAMA_MODEL (может занять несколько минут)..."
            ollama pull "$OLLAMA_MODEL" || warn "Не удалось загрузить — загрузи вручную: ollama pull $OLLAMA_MODEL"
        fi
    fi

    {
        echo "# Локальные секреты — не коммитить в git"
        echo "llm:"
        echo "  provider: \"auto\""
        echo ""
        if [[ -n "$OPENROUTER_KEY" ]]; then
            echo "openrouter:"
            echo "  api_key: \"$OPENROUTER_KEY\""
            echo ""
        fi
        if [[ -n "$GROQ_KEY" ]]; then
            echo "groq:"
            echo "  api_key: \"$GROQ_KEY\""
            echo ""
        fi
        if [[ -n "$OLLAMA_MODEL" ]]; then
            echo "ollama:"
            echo "  model: \"$OLLAMA_MODEL\""
            echo ""
        fi
        if [[ -n "$SALUTE_KEY" ]]; then
            echo "salute_speech:"
            echo "  credentials: \"$SALUTE_KEY\""
            echo "  scope: \"SALUTE_SPEECH_PERS\""
            echo ""
            echo "voice:"
            echo "  engine: \"salute\""
            echo "  voice: \"Nec_24000\""
        else
            echo "voice:"
            echo "  engine: \"silero\""
            echo "  silero_speaker: \"xenia\""
        fi
    } > "$CONFIG_LOCAL"
    success "Создан $CONFIG_LOCAL"
fi  # конец блока config (dry-run / уже существует / создаём)

# ─── 5. Obsidian vault ───────────────────────────────────────────────────────
header "Настройка Obsidian"

CURRENT_OBSIDIAN=$(grep -E "^\s*shared_root:" "$REPO_DIR/config.yaml" | awk '{print $2}' | tr -d '"' | sed "s|~|$HOME|g")
echo "Текущий путь к Obsidian vault: ${CURRENT_OBSIDIAN:-не задан}"

if $DRY_RUN; then
    dry "Спросить путь к Obsidian и дописать в config.local.yaml"
else
    read -rp "Путь к папке Obsidian (Enter — оставить текущий): " OBS_PATH
    if [[ -n "$OBS_PATH" ]]; then
        OBS_PATH="${OBS_PATH/#\~/$HOME}"
        if [[ ! -d "$OBS_PATH" ]]; then
            warn "Папка не найдена: $OBS_PATH"
        else
            {
                echo ""
                echo "obsidian:"
                echo "  shared_root: \"$OBS_PATH\""
                echo "  enabled: true"
            } >> "$CONFIG_LOCAL"
            success "Obsidian vault: $OBS_PATH"
        fi
    fi
fi

# ─── 6. Папки с проектами ────────────────────────────────────────────────────
header "Папки с git-проектами"

DEFAULT_DEV="$HOME/Development"

if $DRY_RUN; then
    dry "Спросить папку с проектами (по умолчанию: $DEFAULT_DEV)"
    dry "Создать папку если не существует"
    dry "Дописать в config.local.yaml"
else
    echo
    echo "Где хранятся твои git-проекты?"
    read -rp "Папка с проектами [${DEFAULT_DEV}]: " DEV_PATH
    DEV_PATH="${DEV_PATH:-$DEFAULT_DEV}"
    DEV_PATH="${DEV_PATH/#\~/$HOME}"

    if [[ ! -d "$DEV_PATH" ]]; then
        mkdir -p "$DEV_PATH"
        success "Создана папка: $DEV_PATH"
    else
        success "Папка проектов: $DEV_PATH"
    fi

    {
        echo ""
        echo "collectors:"
        echo "  git:"
        echo "    scan_dirs:"
        echo "      - \"$DEV_PATH\""
        echo "  filesystem:"
        echo "    watch_dirs:"
        echo "      - \"$DEV_PATH\""
    } >> "$CONFIG_LOCAL"
fi

# ─── 7. Systemd сервис ───────────────────────────────────────────────────────
header "Установка systemd сервиса"

UID_NUM=$(id -u)

if $DRY_RUN; then
    echo
    dry "Записать сервис: $SERVICE_FILE"
    dry "Пути в сервисе:"
    dry "  ExecStart=${VENV_DIR}/bin/python3 ${REPO_DIR}/main.py"
    dry "  WorkingDirectory=${REPO_DIR}"
    if [[ -f "$SERVICE_FILE" ]]; then
        warn "Существующий сервис будет перезаписан (но это безопасно — пути те же)"
    fi
    dry "systemctl --user daemon-reload"
    dry "systemctl --user enable $SERVICE_NAME  (если согласишься)"
    dry "systemctl --user restart $SERVICE_NAME (если согласишься)"
    echo
    success "Dry-run завершён — всё выглядит корректно"
    echo
    echo "Запусти без --dry-run для реальной установки:"
    echo "  bash $REPO_DIR/install.sh"
    exit 0
fi

mkdir -p "$(dirname "$SERVICE_FILE")"

# Генерируем сервис с актуальными путями
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=PC Assistant — локальный AI-ассистент разработчика
Documentation=https://github.com/avelNet/PcAssistent
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${VENV_DIR}/bin/python3 ${REPO_DIR}/main.py
WorkingDirectory=${REPO_DIR}
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=DISPLAY=:0
Environment=XDG_RUNTIME_DIR=/run/user/${UID_NUM}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${UID_NUM}/bus
Environment=PULSE_SERVER=unix:/run/user/${UID_NUM}/pulse/native
Environment=PIPEWIRE_RUNTIME_DIR=/run/user/${UID_NUM}
ExecStartPre=/bin/sleep 5

[Install]
WantedBy=default.target
EOF

success "Сервис записан: $SERVICE_FILE"

systemctl --user daemon-reload

read -rp "Включить автозапуск при входе в систему? [Y/n]: " enable_service
if [[ "${enable_service:-Y}" =~ ^[Yy] ]]; then
    systemctl --user enable "$SERVICE_NAME"
    success "Автозапуск включён"
fi

read -rp "Запустить сервис прямо сейчас? [Y/n]: " start_service
if [[ "${start_service:-Y}" =~ ^[Yy] ]]; then
    systemctl --user start "$SERVICE_NAME"
    sleep 3
    if systemctl --user is-active --quiet "$SERVICE_NAME"; then
        success "Сервис запущен успешно"
    else
        error "Сервис не запустился. Проверь логи:"
        echo "  journalctl --user -u pc-assistant -n 30"
    fi
fi

# ─── 8. Итог ─────────────────────────────────────────────────────────────────
header "Установка завершена"

echo
echo -e "  Статус:    ${BOLD}systemctl --user status pc-assistant${NC}"
echo -e "  Логи:      ${BOLD}journalctl --user -u pc-assistant -f${NC}"
echo -e "  Конфиг:    ${BOLD}$CONFIG_LOCAL${NC}"
echo -e "  Остановить: ${BOLD}systemctl --user stop pc-assistant${NC}"
echo
success "PC Assistant установлен!"
