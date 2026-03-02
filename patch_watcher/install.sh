#!/bin/bash
# install.sh — Install the patch watcher systemd timer.

set -euo pipefail

WATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="/etc/systemd/system"

echo "=== Patch Watcher Installer ==="

# Create log directory
mkdir -p "${HOME}/.patch_watcher"
echo "Created ${HOME}/.patch_watcher/"

# Check that claude CLI is available
if ! command -v claude &>/dev/null; then
	echo "ERROR: 'claude' CLI not found in PATH" >&2
	exit 1
fi

# Check that patches file exists
if [[ ! -f /shared/support_files/patches_to_watch.json ]]; then
	echo "ERROR: /shared/support_files/patches_to_watch.json not found" >&2
	exit 1
fi

# Install systemd units
echo "Installing systemd units..."
sudo cp "$WATCHER_DIR/patch_watcher.service" "$SERVICE_DIR/"
sudo cp "$WATCHER_DIR/patch_watcher.timer" "$SERVICE_DIR/"
sudo systemctl daemon-reload

echo "Enabling timer..."
sudo systemctl enable patch_watcher.timer
sudo systemctl start patch_watcher.timer

echo ""
echo "Timer status:"
systemctl status patch_watcher.timer --no-pager || true

echo ""
echo "=== Installation complete ==="
echo "Timer will fire hourly (with up to 5 min random delay)."
echo "Logs: ${HOME}/.patch_watcher/watcher.log"
echo ""
echo "Manual test: bash $WATCHER_DIR/run_watcher.sh"
echo "Check timer: systemctl list-timers patch_watcher.timer"
echo "Stop timer:  sudo systemctl stop patch_watcher.timer"
