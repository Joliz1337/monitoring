#!/bin/bash
# Creates /usr/local/bin/mon so the command works without full installer run.
# Usage: sudo bash scripts/install-mon-cli.sh

set -e
BIN_PATH="/usr/local/bin/mon"

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0"
    exit 1
fi

script_content='#!/bin/bash
# Monitoring System Manager — auto-update via GitHub

if [ -f /etc/monitoring/proxy.conf ]; then
    . /etc/monitoring/proxy.conf 2>/dev/null
    if [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ]; then
        export http_proxy="$PROXY_URL" https_proxy="$PROXY_URL"
        export HTTP_PROXY="$PROXY_URL" HTTPS_PROXY="$PROXY_URL"
        export no_proxy="localhost,127.0.0.1,::1"
    fi
fi

GITHUB_URL="https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"
TIMEOUT=120

SCRIPT_CONTENT=$(timeout "$TIMEOUT" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT" "$GITHUB_URL" 2>/dev/null)
if [ -n "$SCRIPT_CONTENT" ]; then
    exec bash -c "$SCRIPT_CONTENT" -- "$@"
elif [ -f "/opt/monitoring-panel/install.sh" ]; then
    exec bash "/opt/monitoring-panel/install.sh" "$@"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    exec bash "/opt/monitoring-node/install.sh" "$@"
else
    echo "Failed to download installer from GitHub and no local copy found"
    exit 1
fi'

echo "$script_content" > "$BIN_PATH"
chmod +x "$BIN_PATH"
rm -f /usr/local/bin/monitoring 2>/dev/null || true
echo "Command 'mon' installed at $BIN_PATH. Run: mon"
