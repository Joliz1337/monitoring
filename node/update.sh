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

# Migrate config from old Docker volume to /etc/haproxy (for upgrades from older versions)
migrate_from_docker_volume() {
    # Find old haproxy_config volume
    local volume_name
    volume_name=$(docker volume ls -q | grep "haproxy_config" | head -1)
    
    if [ -z "$volume_name" ]; then
        return 0  # No old volume to migrate
    fi
    
    # Check if host config already exists
    if [ -f "/etc/haproxy/haproxy.cfg" ]; then
        log_info "Host config already exists at /etc/haproxy/haproxy.cfg"
        return 0
    fi
    
    log_info "Found old Docker volume: $volume_name"
    log_info "Migrating config from Docker volume to /etc/haproxy..."
    
    # Extract config from old volume
    local old_config
    old_config=$(docker run --rm -v "$volume_name:/vol:ro" busybox cat /vol/haproxy.cfg 2>/dev/null) || \
    old_config=$(docker run --rm -v "$volume_name:/vol:ro" alpine cat /vol/haproxy.cfg 2>/dev/null) || \
    old_config=""
    
    if [ -n "$old_config" ]; then
        ensure_haproxy_dir
        echo "$old_config" > /etc/haproxy/haproxy.cfg
        chmod 644 /etc/haproxy/haproxy.cfg
        log_success "Config migrated from Docker volume to /etc/haproxy/haproxy.cfg"
        
        # Create backup before removing volume
        cp /etc/haproxy/haproxy.cfg "/tmp/haproxy.cfg.backup.$(date +%Y%m%d_%H%M%S)"
        
        # Remove old volume (will be recreated as bind mount)
        log_info "Removing old Docker volume: $volume_name"
        docker volume rm "$volume_name" 2>/dev/null || true
    else
        log_warn "Could not read config from old volume"
    fi
}

NODE_DIR="/opt/monitoring-node"
TMP_DIR="/tmp/monitoring-update-$$"
TARGET_REF="${1:-main}"

# GitHub mirror for Russia
GITHUB_MIRROR="https://ghfast.top"
ACTIVE_GITHUB_MIRROR=""

# ==================== GitHub Mirror Functions ====================

# Test if GitHub is accessible
test_github_access() {
    log_info "Testing GitHub access..."
    echo -n "  Testing GitHub (direct)... "
    
    if curl -fsSL --connect-timeout 10 --max-time 20 \
        "https://raw.githubusercontent.com/Joliz1337/monitoring/main/VERSION" \
        -o /dev/null 2>/dev/null; then
        echo -e "${GREEN}OK${NC}"
        return 0
    fi
    
    echo -e "${RED}unavailable${NC}"
    return 1
}

# Test if mirror is accessible
test_mirror_access() {
    echo -n "  Testing ghfast.top... "
    
    if curl -fsSL --connect-timeout 10 --max-time 20 \
        "${GITHUB_MIRROR}/https://raw.githubusercontent.com/Joliz1337/monitoring/main/VERSION" \
        -o /dev/null 2>/dev/null; then
        echo -e "${GREEN}OK${NC}"
        return 0
    fi
    
    echo -e "${RED}unavailable${NC}"
    return 1
}

# Select GitHub or mirror
select_best_mirror() {
    if test_github_access; then
        ACTIVE_GITHUB_MIRROR=""
        log_success "Selected: GitHub (direct)"
        return 0
    fi
    
    if test_mirror_access; then
        ACTIVE_GITHUB_MIRROR="$GITHUB_MIRROR"
        log_success "Selected: ghfast.top"
        return 0
    fi
    
    log_warn "All mirrors failed, will try direct GitHub anyway"
    ACTIVE_GITHUB_MIRROR=""
}

# Clone with mirror fallback
clone_with_mirror() {
    local target_dir="$1"
    local branch="$2"
    local repo_url
    
    rm -rf "$target_dir"
    
    if [ -n "$ACTIVE_GITHUB_MIRROR" ]; then
        repo_url="${ACTIVE_GITHUB_MIRROR}/https://github.com/Joliz1337/monitoring.git"
        log_info "Downloading from ghfast.top..."
    else
        repo_url="https://github.com/Joliz1337/monitoring.git"
        log_info "Downloading from GitHub (direct)..."
    fi
    
    if timeout 180 git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir" 2>&1; then
        log_success "Download complete"
        return 0
    fi
    
    # Try the other option
    rm -rf "$target_dir"
    
    if [ -n "$ACTIVE_GITHUB_MIRROR" ]; then
        log_warn "Mirror failed, trying direct GitHub..."
        repo_url="https://github.com/Joliz1337/monitoring.git"
    else
        log_warn "GitHub failed, trying ghfast.top..."
        repo_url="${GITHUB_MIRROR}/https://github.com/Joliz1337/monitoring.git"
    fi
    
    if timeout 180 git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir" 2>&1; then
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

# Select best mirror (GitHub or ghfast.top)
select_best_mirror
echo ""

# Clone repository with mirror fallback
if ! clone_with_mirror "$TMP_DIR" "$TARGET_REF"; then
    log_error "Failed to download repository from all mirrors"
    exit 1
fi

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

# Migrate config from old Docker volume if upgrading from older version
migrate_from_docker_volume

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
