#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
#

set -e

# Prevent interactive prompts during package installation
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
    fi
    exit $exit_code
}
trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Terminated by signal\033[0m"; exit 143' TERM

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Progress Spinner ====================

spin() {
    local desc="$1"; shift
    local logf
    logf=$(mktemp /tmp/.spin-XXXXXX 2>/dev/null || echo "/tmp/.spin-$$")
    local chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local t0
    t0=$(date +%s)

    "$@" >"$logf" 2>&1 &
    local pid=$!
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        local e=$(( $(date +%s) - t0 ))
        local m=$((e / 60)) s=$((e % 60))
        if [ $m -gt 0 ]; then
            printf "\r  \033[0;36m%s\033[0m %s \033[1;33m[%dm %02ds]\033[0m  " \
                "${chars:$((i % 10)):1}" "$desc" "$m" "$s"
        else
            printf "\r  \033[0;36m%s\033[0m %s \033[1;33m[%ds]\033[0m  " \
                "${chars:$((i % 10)):1}" "$desc" "$s"
        fi
        i=$((i + 1))
        sleep 0.12 2>/dev/null || sleep 1
    done

    wait "$pid" 2>/dev/null
    local rc=$?
    local e=$(( $(date +%s) - t0 ))
    printf "\r\033[2K"

    if [ $rc -eq 0 ]; then
        echo -e "  ${GREEN}✓${NC} ${desc} ${CYAN}(${e}s)${NC}"
    else
        echo -e "  ${RED}✗${NC} ${desc} ${RED}— failed after ${e}s${NC}"
        if [ -s "$logf" ]; then
            echo -e "    ${RED}┌── last output ──────────────────────────${NC}"
            tail -15 "$logf" | while IFS= read -r line; do
                echo -e "    ${RED}│${NC} $line"
            done
            echo -e "    ${RED}└─────────────────────────────────────────${NC}"
        fi
    fi

    rm -f "$logf" 2>/dev/null
    return $rc
}

spin_retry() {
    local tmo="$1" retries="$2" delay="$3" desc="$4"
    shift 4

    local attempt=1
    while [ $attempt -le $retries ]; do
        local label="$desc"
        [ "$retries" -gt 1 ] && label="$desc ($attempt/$retries)"

        if spin "$label" timeout "$tmo" "$@"; then
            return 0
        fi

        [ $attempt -lt $retries ] && sleep "$delay"
        attempt=$((attempt + 1))
    done

    return 1
}

# ==================== Proxy Support ====================

load_proxy() {
    local conf="/etc/monitoring/proxy.conf"
    [ -f "$conf" ] || return 0
    . "$conf" 2>/dev/null || return 0
    [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ] || return 0
    export http_proxy="$PROXY_URL" https_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL" HTTPS_PROXY="$PROXY_URL"
    export all_proxy="$PROXY_URL" ALL_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"
    git config --global http.proxy "$PROXY_URL" 2>/dev/null || true
    git config --global https.proxy "$PROXY_URL" 2>/dev/null || true
}
load_proxy

# ==================== Configuration ====================

# Timeouts (in seconds)
DOCKER_PULL_TIMEOUT="${DOCKER_PULL_TIMEOUT:-300}"

# Arguments
TMP_DIR="$1"
PANEL_DIR="$2"
CURRENT_VERSION="$3"

if [ -z "$TMP_DIR" ] || [ -z "$PANEL_DIR" ]; then
    log_error "Usage: apply-update.sh <tmp_dir> <panel_dir> [current_version]"
    exit 1
fi

# Get new version
NEW_VERSION="unknown"
if [ -f "$TMP_DIR/panel/VERSION" ]; then
    NEW_VERSION=$(cat "$TMP_DIR/panel/VERSION")
fi
log_info "Applying update: ${CURRENT_VERSION:-unknown} → $NEW_VERSION"

# Stop containers
log_info "Stopping containers..."
cd "$PANEL_DIR"
docker compose down --timeout 30 || true

# Wait for ports to be released
log_info "Waiting for ports to be released..."
for i in {1..15}; do
    if ! ss -tlnp 2>/dev/null | grep -qE ':(80|443|8000) '; then
        break
    fi
    sleep 1
done
log_success "Containers stopped"

# Copy files (preserve .env, database, generated nginx.conf)
log_info "Copying new files..."
rsync -av --delete \
    --exclude='.env' \
    --exclude='.env.backup' \
    --exclude='backend/.env' \
    --exclude='backend/.env.backup' \
    --exclude='backend/data' \
    --exclude='nginx/nginx.conf' \
    "$TMP_DIR/panel/" "$PANEL_DIR/"

# Restore .env files from backup if they exist
if [ -f "$PANEL_DIR/.env.backup" ]; then
    mv "$PANEL_DIR/.env.backup" "$PANEL_DIR/.env"
    log_success ".env restored"
fi

if [ -f "$PANEL_DIR/backend/.env.backup" ]; then
    mv "$PANEL_DIR/backend/.env.backup" "$PANEL_DIR/backend/.env"
    log_success "backend/.env restored"
fi

# Make scripts executable
chmod +x "$PANEL_DIR"/*.sh 2>/dev/null || true
chmod +x "$PANEL_DIR"/scripts/*.sh 2>/dev/null || true

# Add PostgreSQL settings if missing (older installations)
if [ -f "$PANEL_DIR/.env" ]; then
    if ! grep -q "^POSTGRES_PASSWORD=" "$PANEL_DIR/.env"; then
        log_info "Adding PostgreSQL configuration to .env..."
        POSTGRES_PASSWORD=$(openssl rand -hex 16 2>/dev/null || cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 32)
        cat >> "$PANEL_DIR/.env" << EOF

# PostgreSQL Database (auto-generated)
POSTGRES_USER=panel
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=panel
EOF
        log_success "PostgreSQL configuration added"
    fi
fi

# Regenerate nginx config
log_info "Regenerating nginx configuration..."
if [ -f "$PANEL_DIR/scripts/generate-nginx-config.sh" ]; then
    bash "$PANEL_DIR/scripts/generate-nginx-config.sh" "$PANEL_DIR"
else
    # Inline fallback
    if [ -f "$PANEL_DIR/.env" ]; then
        source "$PANEL_DIR/.env"
        if [ -n "$DOMAIN" ] && [ -n "$PANEL_UID" ] && [ -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
            export DOMAIN PANEL_UID
            envsubst '${DOMAIN} ${PANEL_UID}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"
            log_success "Generated nginx.conf for $DOMAIN"
        fi
    fi
fi

log_success "Files updated"

# Migrate optimization files from old /opt/monitoring-node/ to /opt/monitoring/
if [ -f "/opt/monitoring-node/scripts/network-tune.sh" ] && [ ! -f "/opt/monitoring/scripts/network-tune.sh" ]; then
    mkdir -p /opt/monitoring/scripts 2>/dev/null || true
    mv "/opt/monitoring-node/scripts/network-tune.sh" "/opt/monitoring/scripts/network-tune.sh" 2>/dev/null || true
    rmdir "/opt/monitoring-node/scripts" 2>/dev/null || true
fi
if [ -f "/opt/monitoring-node/configs/VERSION" ] && [ ! -f "/opt/monitoring/configs/VERSION" ]; then
    mkdir -p /opt/monitoring/configs 2>/dev/null || true
    mv "/opt/monitoring-node/configs/VERSION" "/opt/monitoring/configs/VERSION" 2>/dev/null || true
    rmdir "/opt/monitoring-node/configs" 2>/dev/null || true
fi
if [ -f "/etc/systemd/system/network-tune.service" ]; then
    if grep -q "/opt/monitoring-node/scripts/" /etc/systemd/system/network-tune.service 2>/dev/null; then
        sed -i 's|/opt/monitoring-node/scripts/|/opt/monitoring/scripts/|g' /etc/systemd/system/network-tune.service 2>/dev/null || true
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
fi
# Clean up orphan node dir if it has no real installation
if [ -d "/opt/monitoring-node" ] && [ ! -f "/opt/monitoring-node/docker-compose.yml" ]; then
    rmdir "/opt/monitoring-node/scripts" 2>/dev/null || true
    rmdir "/opt/monitoring-node/configs" 2>/dev/null || true
    rmdir "/opt/monitoring-node" 2>/dev/null || true
fi

# Pull new Docker images
cd "$PANEL_DIR"

set +e
# Pull ready images from GHCR (normal flow)
if ! spin_retry 120 2 10 "Pulling Docker images" docker compose pull 2>/dev/null; then
    log_warn "Failed to pull from registry, building locally..."
    spin "Pulling base images" bash -c \
        'docker compose pull --ignore-buildable 2>/dev/null || true'
    spin_retry 600 2 10 "Building images from source" docker compose build || {
        log_error "Failed to build images"
        exit 1
    }
fi
set -e

# Start containers
spin "Starting containers" docker compose up -d || {
    log_error "Failed to start containers"
    exit 1
}

# Wait for panel to be healthy
log_info "Waiting for panel..."
MAX_ATTEMPTS=30
ATTEMPT=0

# Load domain from .env
if [ -f "$PANEL_DIR/.env" ]; then
    source "$PANEL_DIR/.env"
fi

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if curl -sfk "https://localhost/health" > /dev/null 2>&1; then
        log_success "Panel is healthy"
        break
    fi
    if [ -n "$DOMAIN" ] && curl -sfk "https://${DOMAIN}/health" > /dev/null 2>&1; then
        log_success "Panel is healthy"
        break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    sleep 2
done

if [ $ATTEMPT -eq $MAX_ATTEMPTS ]; then
    log_warn "Health check timed out, but containers are running"
fi

# Final version
FINAL_VERSION="unknown"
if [ -f "$PANEL_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$PANEL_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: ${CURRENT_VERSION:-unknown} → $FINAL_VERSION"
log_info "Preserved: .env, database, server list, SSL config"
