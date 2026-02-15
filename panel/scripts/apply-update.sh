#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
#

set -e

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

# Pull new Docker images
log_info "Pulling new Docker images..."
cd "$PANEL_DIR"

set +e
if ! timeout "$DOCKER_PULL_TIMEOUT" docker compose pull 2>&1; then
    log_error "Failed to pull images"
    echo "Check internet access and image availability in the registry"
    exit 1
fi
set -e

log_success "Images pulled"

# Start containers
log_info "Starting containers..."
docker compose up -d
log_success "Containers started"

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
