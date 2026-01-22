#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
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

# Arguments
TMP_DIR="$1"
NODE_DIR="$2"
CURRENT_VERSION="$3"

if [ -z "$TMP_DIR" ] || [ -z "$NODE_DIR" ]; then
    log_error "Usage: apply-update.sh <tmp_dir> <node_dir> [current_version]"
    exit 1
fi

# ==================== HAProxy Functions ====================

ensure_haproxy_dir() {
    if [ ! -d "/etc/haproxy" ]; then
        log_info "Creating /etc/haproxy directory..."
        mkdir -p /etc/haproxy
        chmod 755 /etc/haproxy
    fi
}

check_haproxy_container() {
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^monitoring-haproxy$"; then
        return 0
    fi
    return 1
}

check_native_haproxy() {
    command -v haproxy &>/dev/null
}

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

migrate_haproxy_container_to_native() {
    log_info "=== HAProxy Migration: Container → Native ==="
    
    if ! check_native_haproxy; then
        if ! install_native_haproxy; then
            log_error "Failed to install native HAProxy"
            return 1
        fi
    else
        log_success "Native HAProxy already installed"
    fi
    
    log_info "Stopping HAProxy container..."
    docker stop monitoring-haproxy >/dev/null 2>&1 || true
    docker rm -f monitoring-haproxy >/dev/null 2>&1 || true
    log_success "HAProxy container removed"
    
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

ensure_native_haproxy() {
    if check_native_haproxy; then
        return 0
    fi
    log_info "Native HAProxy not found, installing..."
    install_native_haproxy
}

# ==================== Main Update Logic ====================

# Get new version
NEW_VERSION="unknown"
if [ -f "$TMP_DIR/node/VERSION" ]; then
    NEW_VERSION=$(cat "$TMP_DIR/node/VERSION")
fi
log_info "Applying update: ${CURRENT_VERSION:-unknown} → $NEW_VERSION"

# HAProxy migration check
cd "$NODE_DIR"

if check_haproxy_container; then
    log_warn "Detected old HAProxy container - migrating to native service"
    migrate_haproxy_container_to_native
else
    ensure_native_haproxy
fi

# Stop containers
log_info "Stopping API containers..."
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

# Copy VERSION file
if [ -f "$TMP_DIR/node/VERSION" ]; then
    cp "$TMP_DIR/node/VERSION" "$NODE_DIR/VERSION"
fi

# Restore .env from backup
if [ -f "$NODE_DIR/.env.backup" ]; then
    mv "$NODE_DIR/.env.backup" "$NODE_DIR/.env"
    log_success "Configuration restored"
fi

# Make scripts executable
chmod +x "$NODE_DIR"/*.sh 2>/dev/null || true
chmod +x "$NODE_DIR"/scripts/*.sh 2>/dev/null || true

log_success "Files updated"

# Clean up Docker before build
log_info "Cleaning up Docker cache..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -af > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Rebuild Docker image
log_info "Building new Docker image..."
cd "$NODE_DIR"
docker build --network=host -t monitoring-node-api .
log_success "Image built"

# Ensure /etc/haproxy directory exists
ensure_haproxy_dir

# Start containers
log_info "Starting containers..."
docker compose up -d
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

# Final version
FINAL_VERSION="unknown"
if [ -f "$NODE_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$NODE_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: ${CURRENT_VERSION:-unknown} → $FINAL_VERSION"
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
