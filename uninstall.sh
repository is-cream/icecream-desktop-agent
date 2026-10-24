#!/bin/bash
# Icecream Desktop Agent -- Mac uninstaller.
set -euo pipefail

LABEL="com.icecream.desktop-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
AGENT_DIR="$HOME/.ice-agent"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$AGENT_DIR"

echo "Desktop agent stopped and removed ($AGENT_DIR deleted, token destroyed)."
echo "Note: this does not remove the device from your Tailscale admin console;"
echo "delete it there if you want it gone entirely."
