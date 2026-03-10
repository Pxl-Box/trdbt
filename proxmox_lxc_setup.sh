#!/bin/bash
# Proxmox LXC Helper Script
# Run this inside a fresh Debian/Ubuntu LXC container to set up the Trading 212 bot from GitHub.

set -e

REPO_URL="https://github.com/pxl-box/trdbt.git"
INSTALL_DIR="/opt/trdbt"

echo "=========================================================="
echo " Starting Proxmox LXC Trading Bot Setup                   "
echo "=========================================================="

echo "[1/4] Updating system packages & installing dependencies..."
apt-get update -y
apt-get install -y git python3 python3-venv python3-pip curl

echo "[2/4] Cloning the repository from GitHub..."
if [ -d "$INSTALL_DIR" ]; then
    echo "Directory $INSTALL_DIR already exists. Pulling latest changes..."
    cd $INSTALL_DIR
    git pull
else
    git clone $REPO_URL $INSTALL_DIR
    cd $INSTALL_DIR
fi

echo "[3/4] Running application setup script..."
bash setup.sh

echo "=========================================================="
echo " Setup Complete!                                          "
echo " You can now navigate to http://<LXC_IP>:8501             "
echo " The bot daemon (tradingbot.service) has been configured. "
echo " Remember to input your Trading 212 API Key in the UI!    "
echo "=========================================================="
