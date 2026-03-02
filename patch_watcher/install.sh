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
sudo cp "$WATCHER_DIR/patch_watcher_daily.service" "$SERVICE_DIR/"
sudo cp "$WATCHER_DIR/patch_watcher_daily.timer" "$SERVICE_DIR/"
sudo systemctl daemon-reload

echo "Enabling hourly watcher timer..."
sudo systemctl enable patch_watcher.timer
sudo systemctl start patch_watcher.timer

echo "Enabling daily confidence report timer..."
sudo systemctl enable patch_watcher_daily.timer
sudo systemctl start patch_watcher_daily.timer

echo ""
echo "Timer status:"
systemctl list-timers 'patch_watcher*' --no-pager || true

echo ""
echo "=== Installation complete ==="
echo "Hourly watcher: checks patches, emails on actions."
echo "Daily report:   confidence summary at 08:00."
echo "Logs: ${HOME}/.patch_watcher/watcher.log"
echo ""
echo "Manual test:    bash $WATCHER_DIR/run_watcher.sh"
echo "Manual daily:   bash $WATCHER_DIR/daily_confidence.sh"
echo "Check timers:   systemctl list-timers 'patch_watcher*'"
echo "Stop all:       sudo systemctl stop patch_watcher.timer patch_watcher_daily.timer"
