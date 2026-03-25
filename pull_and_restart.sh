#!/bin/bash
# pull_and_restart.sh
# Purpose: Pull latest changes from git and restart tradingbot + tradingui services.

set -e # Exit on error

# Navigate to script directory
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$APP_DIR"

echo "Step 1: Pulling latest changes from Git..."
git pull

echo "Step 2: Restarting Services..."
if [ "$EUID" -eq 0 ]; then
    systemctl restart tradingbot.service
    systemctl restart tradingui.service
else
    echo "Using sudo to restart services..."
    sudo systemctl restart tradingbot.service
    sudo systemctl restart tradingui.service
fi

echo "✅ Services restarted successfully."
echo ""
echo "Current status of tradingbot:"
systemctl status tradingbot.service --no-pager | grep "Active:"
echo "Current status of tradingui:"
systemctl status tradingui.service --no-pager | grep "Active:"
