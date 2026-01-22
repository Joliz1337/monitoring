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

# Ensure /etc/haproxy directory exists on host for bind mount
ensure_haproxy_dir() {
    if [ ! -d "/etc/haproxy" ]; then
        log_info "Creating /etc/haproxy directory..."
        mkdir -p /etc/haproxy
        chmod 755 /etc/haproxy
    fi
}

# ==================== HAProxy Migration Functions ====================

# Check if HAProxy container exists (old version)
check_haproxy_container() {
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^monitoring-haproxy$"; then
        return 0
    fi
    return 1
}

# Check if native HAProxy is installed
check_native_haproxy() {
    command -v haproxy &>/dev/null
}

# Install native HAProxy
install_native_haproxy() {
    log_info "Installing native HAProxy..."
    apt-get update -qq >/dev/null 2>&1 || true
    if apt-get install -y -qq haproxy >/dev/null 2>&1; then
        log_success "HAProxy installed"
        return 0
    else
        log_error "Failed to install HAProxy"
        return 1
    fi
}

# Migrate from container HAProxy to native HAProxy
# Config is already on host (/etc/haproxy/haproxy.cfg) - container uses bind mount
migrate_haproxy_container_to_native() {
    log_info "=== HAProxy Migration: Container → Native ==="
    
    # 1. Install native HAProxy if not installed
    if ! check_native_haproxy; then
        if ! install_native_haproxy; then
            log_error "Failed to install native HAProxy"
            return 1
        fi
    else
        log_success "Native HAProxy already installed"
    fi
    
    # 2. Stop and remove container HAProxy
    log_info "Stopping HAProxy container..."
    docker stop monitoring-haproxy >/dev/null 2>&1 || true
    docker rm -f monitoring-haproxy >/dev/null 2>&1 || true
    log_success "HAProxy container removed"
    
    # 3. Enable and start native HAProxy
    # Config is already at /etc/haproxy/haproxy.cfg (was bind-mounted to container)
    if [ -f "/etc/haproxy/haproxy.cfg" ]; then
        log_info "Validating HAProxy config..."
        if haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null 2>&1; then
            log_success "Config is valid"
            
            log_info "Enabling native HAProxy service..."
            systemctl enable haproxy >/dev/null 2>&1 || true
            
            log_info "Starting native HAProxy service..."
            systemctl start haproxy >/dev/null 2>&1 || true
            
            if systemctl is-active --quiet haproxy; then
                log_success "Native HAProxy started successfully"
            else
                log_warn "HAProxy failed to start - check: journalctl -u haproxy"
            fi
        else
            log_warn "HAProxy config is invalid - service not started"
            log_warn "Fix config and start manually: systemctl start haproxy"
        fi
    else
        log_info "No HAProxy config found - service not started"
        log_info "Configure HAProxy via panel or manually"
    fi
    
    log_success "=== HAProxy Migration Complete ==="
    return 0
}

# Ensure native HAProxy is installed
ensure_native_haproxy() {
    if check_native_haproxy; then
        return 0
    fi
    log_info "Native HAProxy not found, installing..."
    install_native_haproxy
}

NODE_DIR="/opt/monitoring-node"
TMP_DIR="/tmp/monitoring-update-$$"
TARGET_REF="${1:-main}"

# GitHub mirror
GITHUB_MIRROR="https://ghfast.top"

# ==================== GitHub Clone Functions ====================

# Clone repository: GitHub first (30s timeout), fallback to mirror
clone_with_fallback() {
    local target_dir="$1"
    local branch="$2"
    
    rm -rf "$target_dir"
    
    # Try direct GitHub first (30 second timeout)
    log_info "Downloading from GitHub..."
    if timeout 30 git clone --depth 1 --branch "$branch" "https://github.com/Joliz1337/monitoring.git" "$target_dir" 2>&1; then
        log_success "Download complete"
        return 0
    fi
    
    # GitHub failed, try mirror
    rm -rf "$target_dir"
    log_warn "GitHub timeout/error, trying mirror (ghfast.top)..."
    
    if timeout 120 git clone --depth 1 --branch "$branch" "${GITHUB_MIRROR}/https://github.com/Joliz1337/monitoring.git" "$target_dir" 2>&1; then
        log_success "Download complete"
        return 0
    fi
    
    return 1
}

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

# Clone repository (GitHub first, then mirror fallback)
if ! clone_with_fallback "$TMP_DIR" "$TARGET_REF"; then
    log_error "Failed to download repository"
    exit 1
fi

# Check if download succeeded
if [ ! -d "$TMP_DIR/node" ]; then
    log_error "Failed to download repository"
    exit 1
fi

# Get new version
NEW_VERSION="unknown"
if [ -f "$TMP_DIR/node/VERSION" ]; then
    NEW_VERSION=$(cat "$TMP_DIR/node/VERSION")
fi
log_info "New version: $NEW_VERSION"

# ==================== HAProxy Migration Check ====================
# Check if upgrading from old version with HAProxy container
cd "$NODE_DIR"

if check_haproxy_container; then
    log_warn "Detected old HAProxy container - migrating to native service"
    migrate_haproxy_container_to_native
else
    # Ensure native HAProxy is installed (for fresh updates without container)
    ensure_native_haproxy
fi

# ==================== Stop Containers ====================
log_info "Stopping API containers..."

# Note: HAProxy is now a native systemd service, not a container
# We don't touch it during update to avoid disruption

docker compose down --timeout 30 || true

# Wait for port 7500 to be released
log_info "Waiting for port 7500 to be released..."
for i in {1..15}; do
    if ! ss -tlnp 2>/dev/null | grep -q ':7500 '; then
        break
    fi
    sleep 1
done
log_success "Containers stopped"

# Copy files (preserve .env and SSL certs)
log_info "Copying new files..."
rsync -av --delete \
    --exclude='.env' \
    --exclude='.env.backup' \
    --exclude='nginx/ssl' \
    "$TMP_DIR/node/" "$NODE_DIR/"

# Copy VERSION file from node directory
if [ -f "$TMP_DIR/node/VERSION" ]; then
    cp "$TMP_DIR/node/VERSION" "$NODE_DIR/VERSION"
fi

# Restore .env
if [ -f "$NODE_DIR/.env.backup" ]; then
    mv "$NODE_DIR/.env.backup" "$NODE_DIR/.env"
    log_success "Configuration restored"
fi

# Make scripts executable
chmod +x "$NODE_DIR"/*.sh 2>/dev/null || true

log_success "Files updated"

# Clean up Docker before build to free space
log_info "Cleaning up Docker cache..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -af > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Rebuild Docker image
log_info "Building new Docker image..."
cd "$NODE_DIR"
# Use --network=host to bypass Docker network isolation issues during build
docker build --network=host -t monitoring-node-api .
log_success "Image built"

# Ensure /etc/haproxy directory exists for bind mount
ensure_haproxy_dir

# Start containers (API and nginx only - HAProxy is native systemd service)
log_info "Starting containers..."
docker compose up -d

log_success "Containers started"

# Note: HAProxy runs as native systemd service
# Check status with: systemctl status haproxy

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
log_info "Preserved: .env, nginx/ssl, /etc/haproxy config, /etc/letsencrypt certs, traffic data"

# Show HAProxy status
if systemctl is-active --quiet haproxy 2>/dev/null; then
    log_success "HAProxy (native service): Running"
elif command -v haproxy &>/dev/null; then
    log_warn "HAProxy (native service): Installed but not running"
    log_info "Start with: systemctl start haproxy"
else
    log_warn "HAProxy: Not installed"
    log_info "Install with: apt install haproxy"
fi
