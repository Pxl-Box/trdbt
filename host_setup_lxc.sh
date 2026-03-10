#!/bin/bash
# Proxmox Host Script
# Run this on your Proxmox Host Shell (Datacenter -> Node -> Shell) to automatically
# create an LXC container and deploy the Trading 212 bot inside it.

set -e

echo "=========================================================="
echo " Proxmox Host: Automated LXC & Trading Bot Setup          "
echo "=========================================================="

# 1. Get next available VMID
VMID=$(pvesh get /cluster/nextid)
echo "[INFO] Next available VMID is: $VMID"

# 2. Get latest Debian 12 Template
echo "[INFO] Updating Proxmox appliance templates..."
pveam update >/dev/null

TEMPLATE=$(pveam available -section system | grep "debian-12-standard" | awk '{print $2}' | head -n1)
if [ -z "$TEMPLATE" ]; then
    echo "[ERROR] Could not find a Debian 12 template. Aborting."
    exit 1
fi

echo "[INFO] Downloading template: $TEMPLATE to local storage..."
pveam download local $TEMPLATE || true # Ignore if already exists

# 3. Create the LXC
echo "[INFO] Creating LXC VMID $VMID..."
# Default 2GB RAM, 1 Core, 8GB Disk on local-lvm. 
# Adjust storage ('local-lvm') if your Proxmox uses a different default like 'zfs' or 'local'.
pct create $VMID local:vztmpl/${TEMPLATE##*/} \
    -arch amd64 \
    -hostname tradingbot \
    -cores 1 \
    -memory 2048 \
    -net0 name=eth0,bridge=vmbr0,ip=dhcp \
    -storage local-lvm \
    -unprivileged 1 \
    -features nesting=1 \
    -password "TradingBot123!"

echo "[INFO] Starting LXC $VMID..."
pct start $VMID

# Wait for container network to initialize
echo "[INFO] Waiting 10 seconds for container network to initialize..."
sleep 10

# 4. Bootstrap the inside of the container
echo "[INFO] Bootstrapping Trading Bot inside the container..."

# Download and run the inner setup script directly via pct exec
pct exec $VMID -- bash -c "apt-get update -y && apt-get install -y curl && curl -sSL https://raw.githubusercontent.com/pxl-box/trdbt/main/proxmox_lxc_setup.sh -o /tmp/setup.sh && bash /tmp/setup.sh"

echo "=========================================================="
echo " Automated Setup Complete!                                "
echo " LXC ID: $VMID"
echo " OS: Debian 12"
echo " Password: TradingBot123!"
echo " "
echo " To find the IP of the dashboard, run:"
echo " pct exec $VMID -- ip -4 a show eth0 | grep inet"
echo " Then navigate to http://<IP>:8501"
echo "=========================================================="
