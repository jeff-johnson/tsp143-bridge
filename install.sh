#!/bin/bash
# Copyright (c) 2026 Senternal LLC <https://senternaltech.com>
# SPDX-License-Identifier: MIT
set -euo pipefail

INSTALL_DIR="/opt/tsp143-bridge"
SERVICE_USER="tsp-bridge"

echo "=== TSP143IIIU Wi-Fi Print Bridge Installer ==="

if [[ $EUID -ne 0 ]]; then
    echo "Error: Run this script as root (sudo ./install.sh)"
    exit 1
fi

if [[ ! -e /dev/usb/lp0 ]]; then
    echo "Warning: /dev/usb/lp0 not found. Is the TSP143IIIU connected via USB?"
    echo "Continuing anyway — the service will retry when the printer is connected."
fi

echo "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq python3

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG lp "$SERVICE_USER"

echo "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp print_server_bidir.py star_discovery.py stario_proto.py stario_client.py \
    run.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/run.sh"

echo "Setting up systemd service..."
cp tsp143-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable tsp143-bridge
systemctl restart tsp143-bridge

echo ""
echo "=== Installation complete ==="
systemctl status tsp143-bridge --no-pager || true
echo ""
echo "View logs with: journalctl -u tsp143-bridge -f"
echo ""
echo "Next steps:"
echo "  1. Ensure the Pi is on the same Wi-Fi network as your iPad."
echo "  2. Open Shopify POS > Settings > Hardware > Printers."
echo "  3. The printer should appear automatically as 'TSP143III'."
echo "  4. If not, add it manually using this Pi's IP address:"
ip -4 addr show wlan0 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1 \
    | xargs -I{} echo "     Pi IP address: {}"
