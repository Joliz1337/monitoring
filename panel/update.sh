#!/bin/bash
#
# Panel Update Script - Simple and reliable
# Updates monitoring panel from GitHub repository
#
# Usage: ./update.sh [commit_hash|tag|branch]
#   If no argument provided, updates to latest commit from main branch
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

REPO_URL="https://github.com/Joliz1337/monitoring.git"
PANEL_DIR="/opt/monitoring-panel"
TMP_DIR="/tmp/panel-update-$$"
TARGET_REF="${1:-main}"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log_info "=== Monitoring Panel Update ==="
log_info "Target: $TARGET_REF"
log_info "Panel directory: $PANEL_DIR"

# Check Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker not found"
    exit 1
fi

if ! docker info &> /dev/null; then
    log_error "Cannot connect to Docker daemon"
    exit 1
fi

# Check panel directory exists
if [ ! -f "$PANEL_DIR/docker-compose.yml" ]; then
    log_error "Panel installation not found at $PANEL_DIR"
    exit 1
fi

# Get current version
CURRENT_VERSION="unknown"
if [ -f "$PANEL_DIR/VERSION" ]; then
    CURRENT_VERSION=$(cat "$PANEL_DIR/VERSION")
fi
log_info "Current version: $CURRENT_VERSION"

# Backup .env files
log_info "Backing up configuration..."
if [ -f "$PANEL_DIR/.env" ]; then
    cp "$PANEL_DIR/.env" "$PANEL_DIR/.env.backup"
    log_success ".env backed up"
fi

if [ -f "$PANEL_DIR/backend/.env" ]; then
    cp "$PANEL_DIR/backend/.env" "$PANEL_DIR/backend/.env.backup"
    log_success "backend/.env backed up"
fi

# Clone repository
log_info "Downloading from GitHub..."
rm -rf "$TMP_DIR"
git clone --depth 1 --branch "$TARGET_REF" "$REPO_URL" "$TMP_DIR" 2>&1

# Check if download succeeded
if [ ! -d "$TMP_DIR/panel" ]; then
    log_error "Failed to download repository"
    exit 1
fi

# Get new version
NEW_VERSION="unknown"
if [ -f "$TMP_DIR/panel/VERSION" ]; then
    NEW_VERSION=$(cat "$TMP_DIR/panel/VERSION")
fi
log_info "New version: $NEW_VERSION"

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

# VERSION file is already copied with rsync from panel/VERSION

# Restore .env files
if [ -f "$PANEL_DIR/.env.backup" ]; then
    mv "$PANEL_DIR/.env.backup" "$PANEL_DIR/.env"
    log_success ".env restored"
fi

if [ -f "$PANEL_DIR/backend/.env.backup" ]; then
    mv "$PANEL_DIR/backend/.env.backup" "$PANEL_DIR/backend/.env"
    log_success "backend/.env restored"
fi

# Regenerate nginx config from template if domain is set
if [ -f "$PANEL_DIR/.env" ]; then
    source "$PANEL_DIR/.env"
    if [ -n "$DOMAIN" ] && [ -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
        export DOMAIN
        envsubst '${DOMAIN}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"
        log_success "Regenerated nginx.conf for $DOMAIN"
    fi
fi

# Make scripts executable
chmod +x "$PANEL_DIR"/*.sh 2>/dev/null || true

log_success "Files updated"

# Clean up Docker before build to free space
log_info "Cleaning up Docker cache..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -af > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Rebuild Docker images
log_info "Building new Docker images..."
cd "$PANEL_DIR"
docker compose build --no-cache
log_success "Images built"

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

# Final version check
FINAL_VERSION="unknown"
if [ -f "$PANEL_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$PANEL_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: $CURRENT_VERSION → $FINAL_VERSION"
log_info "Preserved: .env, database, server list, SSL config"
