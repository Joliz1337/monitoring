#!/bin/bash
#
# Generate nginx.conf from template
# This script is called from update.sh to ensure latest version is used
#

PANEL_DIR="${1:-/opt/monitoring-panel}"

# Load environment
if [ -f "$PANEL_DIR/.env" ]; then
    source "$PANEL_DIR/.env"
fi

# Validate required variables
if [ -z "$DOMAIN" ]; then
    echo "[ERROR] DOMAIN variable is empty!"
    exit 1
fi

if [ -z "$PANEL_UID" ]; then
    echo "[ERROR] PANEL_UID variable is empty!"
    exit 1
fi

# Check template exists
if [ ! -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
    echo "[ERROR] nginx.conf.template not found!"
    exit 1
fi

# Generate config
export DOMAIN PANEL_UID
envsubst '${DOMAIN} ${PANEL_UID}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"

echo "[OK] Generated nginx.conf for $DOMAIN with UID protection"
