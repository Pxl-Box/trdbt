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

echo "Configuring systemd daemon (tradingbot.service)..."
SERVICE_FILE="/etc/systemd/system/tradingbot.service"

cp $APP_DIR/tradingbot.service /tmp/tradingbot.service
sed -i "s|/opt/trdbt|$APP_DIR|g" /tmp/tradingbot.service

if [ "$EUID" -eq 0 ]; then
    cp /tmp/tradingbot.service $SERVICE_FILE
    systemctl daemon-reload
    systemctl enable tradingbot.service
    systemctl restart tradingbot.service
    echo "Daemon installed and started."
else
    echo "Please run this script as root (or use sudo) to install the systemd service automatically."
    echo "Alternatively, you can manually copy tradingbot.service to /etc/systemd/system/ and enable it."
fi

echo "To start the Web Dashboard manually, run:"
echo "source venv/bin/activate && streamlit run app.py"
