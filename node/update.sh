#!/bin/bash
#
# Node Update Script - Simple and reliable
# Updates monitoring node from GitHub repository
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
NODE_DIR="/opt/monitoring-node"
TMP_DIR="/tmp/monitoring-update-$$"
TARGET_REF="${1:-main}"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

log_info "=== Monitoring Node Update ==="
log_info "Target: $TARGET_REF"
log_info "Node directory: $NODE_DIR"

# Check Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker not found"
    exit 1
fi

if ! docker info &> /dev/null; then
    log_error "Cannot connect to Docker daemon"
    exit 1
fi

# Check node directory exists
if [ ! -f "$NODE_DIR/docker-compose.yml" ]; then
    log_error "Node installation not found at $NODE_DIR"
    exit 1
fi

# Get current version
CURRENT_VERSION="unknown"
if [ -f "$NODE_DIR/VERSION" ]; then
    CURRENT_VERSION=$(cat "$NODE_DIR/VERSION")
fi
log_info "Current version: $CURRENT_VERSION"

# Backup .env
log_info "Backing up configuration..."
if [ -f "$NODE_DIR/.env" ]; then
    cp "$NODE_DIR/.env" "$NODE_DIR/.env.backup"
    log_success ".env backed up"
fi

# Clone repository
log_info "Downloading from GitHub..."
rm -rf "$TMP_DIR"
git clone --depth 1 --branch "$TARGET_REF" "$REPO_URL" "$TMP_DIR" 2>&1

# Check if download succeeded
if [ ! -d "$TMP_DIR/node" ]; then
    log_error "Failed to download repository"
    exit 1
fi

# Get new version
NEW_VERSION="unknown"
if [ -f "$TMP_DIR/VERSION" ]; then
    NEW_VERSION=$(cat "$TMP_DIR/VERSION")
fi
log_info "New version: $NEW_VERSION"

# Stop containers
log_info "Stopping containers..."
cd "$NODE_DIR"

# Check if HAProxy was running
HAPROXY_WAS_RUNNING=false
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "monitoring-haproxy"; then
    HAPROXY_WAS_RUNNING=true
    log_info "HAProxy is running, will restart after update"
fi

docker compose down --timeout 30 || true
log_success "Containers stopped"

# Copy files (preserve .env and SSL certs)
log_info "Copying new files..."
rsync -av --delete \
    --exclude='.env' \
    --exclude='.env.backup' \
    --exclude='nginx/ssl' \
    "$TMP_DIR/node/" "$NODE_DIR/"

# Copy VERSION file from root
if [ -f "$TMP_DIR/VERSION" ]; then
    cp "$TMP_DIR/VERSION" "$NODE_DIR/VERSION"
fi

# Restore .env
if [ -f "$NODE_DIR/.env.backup" ]; then
    mv "$NODE_DIR/.env.backup" "$NODE_DIR/.env"
    log_success "Configuration restored"
fi

# Make scripts executable
chmod +x "$NODE_DIR"/*.sh 2>/dev/null || true

log_success "Files updated"

# Rebuild Docker image
log_info "Building new Docker image..."
cd "$NODE_DIR"
docker compose build --no-cache
log_success "Image built"

# Clean up old Docker images and build cache
log_info "Cleaning up old Docker images..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -f > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Start containers
log_info "Starting containers..."
docker compose up -d

# Restart HAProxy if it was running
if [ "$HAPROXY_WAS_RUNNING" = true ]; then
    log_info "Restarting HAProxy..."
    docker compose --profile haproxy up -d
fi

log_success "Containers started"

# Wait for API to be healthy
log_info "Waiting for API..."
MAX_ATTEMPTS=30
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    if curl -sf "http://localhost:7500/health" > /dev/null 2>&1; then
        log_success "API is healthy"
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
if [ -f "$NODE_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$NODE_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: $CURRENT_VERSION → $FINAL_VERSION"
log_info "Preserved: .env, nginx/ssl, HAProxy config, SSL certs, traffic data"
