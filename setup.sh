#!/bin/bash
# Local Environment Setup Script
# Configures the virtual environment, installs dependencies, and creates the systemd service.

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

echo "Setting up Trading Bot Environment at: $APP_DIR"

if [ ! -d "$APP_DIR/venv" ]; then
    echo "Creating Python Virtual Environment..."
    python3 -m venv $APP_DIR/venv
fi

echo "Installing Python dependencies..."
$APP_DIR/venv/bin/pip install --upgrade pip
$APP_DIR/venv/bin/pip install -r $APP_DIR/requirements.txt

echo "Configuring firewall for Streamlit Dashboard (Port 8501)..."
if command -v ufw > /dev/null; then
    ufw allow 8501/tcp
else
    echo "ufw not found. Assuming firewall is managed externally."
fi

echo "Configuring systemd daemons (tradingbot.service & tradingui.service)..."
SERVICE_FILE_BOT="/etc/systemd/system/tradingbot.service"
SERVICE_FILE_UI="/etc/systemd/system/tradingui.service"

cp $APP_DIR/tradingbot.service /tmp/tradingbot.service
sed -i "s|/opt/trdbt|$APP_DIR|g" /tmp/tradingbot.service

cp $APP_DIR/tradingui.service /tmp/tradingui.service
sed -i "s|/opt/trdbt|$APP_DIR|g" /tmp/tradingui.service

if [ "$EUID" -eq 0 ]; then
    cp /tmp/tradingbot.service $SERVICE_FILE_BOT
    cp /tmp/tradingui.service $SERVICE_FILE_UI
    systemctl daemon-reload
    systemctl enable tradingbot.service
    systemctl enable tradingui.service
    systemctl restart tradingbot.service
    systemctl restart tradingui.service
    echo "Daemons installed and started."
else
    echo "Please run this script as root (or use sudo) to install the systemd services automatically."
    echo "Alternatively, you can manually copy tradingbot.service and tradingui.service to /etc/systemd/system/ and enable them."
fi

echo "To check the bot logs locally, run:"
echo "pct exec <VMID> -- journalctl -u tradingbot --no-pager | tail -n 20"
echo "To check the streamlit UI logs, run:"
echo "pct exec <VMID> -- journalctl -u tradingui --no-pager | tail -n 20"
