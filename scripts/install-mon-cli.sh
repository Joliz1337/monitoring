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

# Установщик больше 128 КБ — лимита ядра на длину одного аргумента (MAX_ARG_STRLEN),
# поэтому передать его текстом в bash -c нельзя: exec упадёт с E2BIG.
TMP_INSTALLER="$(mktemp /tmp/mon-installer.XXXXXX)"
trap "rm -f $TMP_INSTALLER" EXIT

if timeout "$TIMEOUT" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT" -o "$TMP_INSTALLER" "$GITHUB_URL" 2>/dev/null && [ -s "$TMP_INSTALLER" ]; then
    INSTALLER="$TMP_INSTALLER"
elif [ -f "/opt/monitoring-panel/install.sh" ]; then
    INSTALLER="/opt/monitoring-panel/install.sh"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    INSTALLER="/opt/monitoring-node/install.sh"
else
    echo "Failed to download installer from GitHub and no local copy found"
    exit 1
fi

bash "$INSTALLER" "$@"'

echo "$script_content" > "$BIN_PATH"
chmod +x "$BIN_PATH"
rm -f /usr/local/bin/monitoring 2>/dev/null || true
echo "Command 'mon' installed at $BIN_PATH. Run: mon"
