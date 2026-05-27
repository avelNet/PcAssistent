#!/bin/bash
# Установка pc-assistant как systemd user service
# Запускать: bash systemd/install.sh

set -e

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/pc-assistant.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "📦 Устанавливаем pc-assistant.service..."

mkdir -p "$SERVICE_DIR"
cp "$SCRIPT_DIR/pc-assistant.service" "$SERVICE_FILE"

# Перезагружаем демон
systemctl --user daemon-reload

# Включаем автозапуск
systemctl --user enable pc-assistant.service

echo "✅ Сервис установлен и включён"
echo ""
echo "Команды управления:"
echo "  systemctl --user start pc-assistant     # запустить сейчас"
echo "  systemctl --user stop pc-assistant      # остановить"
echo "  systemctl --user status pc-assistant    # статус"
echo "  journalctl --user -u pc-assistant -f    # логи в реальном времени"
echo ""
echo "Запустить сейчас?"
read -p "[y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    systemctl --user start pc-assistant.service
    echo "✅ Запущен"
fi
