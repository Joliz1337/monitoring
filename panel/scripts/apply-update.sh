#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
#
# Environment variables:
#   UPDATE_PROXY - HTTP proxy (passed from update.sh, used for logging)
#

set -e

# Build log file for error reporting
BUILD_LOG="/tmp/docker_build_$$.log"

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
        if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
            echo -e "\033[0;31m[ERROR] Last 30 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -30 "$BUILD_LOG"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
        fi
        rm -f "$BUILD_LOG"
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
DOCKER_BUILD_TIMEOUT="${DOCKER_BUILD_TIMEOUT:-1800}"  # 30 min default

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

# Log proxy if used
if [ -n "$UPDATE_PROXY" ]; then
    log_info "Using proxy: $UPDATE_PROXY"
fi

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

# Enable BuildKit for faster builds with cache
export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

# Rebuild Docker images with timeout
log_info "Building new Docker images (timeout: ${DOCKER_BUILD_TIMEOUT}s)..."
cd "$PANEL_DIR"

# Generate cache bust hash from .env (forces rebuild when any config changes)
CACHE_BUST=""
if [ -f "$PANEL_DIR/.env" ]; then
    CACHE_BUST=$(md5sum "$PANEL_DIR/.env" | cut -d' ' -f1)
    export CACHE_BUST
    log_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
fi

# Run build in background, capture output to log file
set +e
timeout "$DOCKER_BUILD_TIMEOUT" docker compose build --parallel --build-arg CACHE_BUST=${CACHE_BUST} > "$BUILD_LOG" 2>&1 &
BUILD_PID=$!

# Show progress while building (last 30 lines of log)
while kill -0 $BUILD_PID 2>/dev/null; do
    if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
        # Clear screen and show last 30 lines
        clear
        echo -e "${CYAN}[INFO]${NC} Building Docker images... (press Ctrl+C to cancel)"
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        tail -30 "$BUILD_LOG" 2>/dev/null
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    fi
    sleep 3
done
echo ""

wait $BUILD_PID
BUILD_EXIT_CODE=$?
set -e

if [ $BUILD_EXIT_CODE -eq 0 ]; then
    log_success "Images built"
    rm -f "$BUILD_LOG"
elif [ $BUILD_EXIT_CODE -eq 124 ]; then
    log_error "Build timeout after ${DOCKER_BUILD_TIMEOUT}s"
    echo "Try increasing timeout: export DOCKER_BUILD_TIMEOUT=3600"
    echo "Or check server memory: free -h"
    echo ""
    echo -e "${YELLOW}Last 30 lines of build output:${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    tail -30 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
else
    log_error "Build failed (exit code: $BUILD_EXIT_CODE)"
    echo ""
    echo -e "${YELLOW}Last 30 lines of build output:${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    tail -30 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
fi

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
