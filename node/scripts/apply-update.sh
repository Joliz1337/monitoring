#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
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
            echo -e "\033[0;31m[ERROR] Last 50 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -50 "$BUILD_LOG"
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
APT_TIMEOUT="${APT_TIMEOUT:-120}"
PIP_TIMEOUT="${PIP_TIMEOUT:-120}"

# Best mirrors (will be detected)
BEST_PYPI_MIRROR=""
BEST_APT_MIRROR=""

# Arguments
TMP_DIR="$1"
NODE_DIR="$2"
CURRENT_VERSION="$3"

if [ -z "$TMP_DIR" ] || [ -z "$NODE_DIR" ]; then
    log_error "Usage: apply-update.sh <tmp_dir> <node_dir> [current_version]"
    exit 1
fi

# ==================== Mirror Speed Testing ====================

test_mirror_speed() {
    local url="$1"
    local timeout_sec="${2:-5}"
    local start_time end_time elapsed
    
    start_time=$(date +%s%N 2>/dev/null || date +%s)
    if curl -fsSL --connect-timeout "$timeout_sec" --max-time "$timeout_sec" "$url" >/dev/null 2>&1; then
        end_time=$(date +%s%N 2>/dev/null || date +%s)
        if [[ "$start_time" =~ ^[0-9]{10,}$ ]]; then
            elapsed=$(( (end_time - start_time) / 1000000 ))
        else
            elapsed=$(( (end_time - start_time) * 1000 ))
        fi
        echo "$elapsed"
    else
        echo "9999"
    fi
}

detect_best_pypi_mirror() {
    log_info "Testing PyPI mirrors..."
    local best_mirror="https://pypi.org/simple"
    local best_time=9999
    local test_urls=(
        "https://pypi.org/simple/pip/"
        "https://pypi.tuna.tsinghua.edu.cn/simple/pip/"
        "https://mirrors.aliyun.com/pypi/simple/pip/"
    )
    local mirrors=(
        "https://pypi.org/simple"
        "https://pypi.tuna.tsinghua.edu.cn/simple"
        "https://mirrors.aliyun.com/pypi/simple"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || time_ms=9999
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_PYPI_MIRROR="$best_mirror"
    [ "$best_time" -lt 9999 ] && log_success "Best PyPI: $best_mirror (${best_time}ms)"
}

detect_best_apt_mirror() {
    log_info "Testing APT mirrors..."
    local best_mirror="mirror.yandex.ru"
    local best_time=9999
    local test_urls=(
        "http://deb.debian.org/debian/dists/stable/Release"
        "http://mirror.yandex.ru/debian/dists/stable/Release"
        "http://mirrors.aliyun.com/debian/dists/stable/Release"
    )
    local mirrors=(
        "deb.debian.org"
        "mirror.yandex.ru"
        "mirrors.aliyun.com"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || time_ms=9999
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_APT_MIRROR="$best_mirror"
    [ "$best_time" -lt 9999 ] && log_success "Best APT: $best_mirror (${best_time}ms)"
}

detect_best_mirrors() {
    detect_best_pypi_mirror
    detect_best_apt_mirror
}

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

wait_for_apt_lock() {
    local max_wait=60
    local waited=0
    
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
        if [ $waited -eq 0 ]; then
            log_warn "Waiting for apt lock (another process is using apt)..."
        fi
        sleep 2
        waited=$((waited + 2))
        if [ $waited -ge $max_wait ]; then
            log_error "Timeout waiting for apt lock after ${max_wait}s"
            return 1
        fi
    done
    return 0
}

install_native_haproxy() {
    log_info "Installing native HAProxy..."
    
    # Wait for apt lock if needed
    if ! wait_for_apt_lock; then
        log_error "Cannot acquire apt lock"
        return 1
    fi
    
    # Update with timeout (2 minutes max)
    log_info "Updating package lists (timeout: 120s)..."
    if ! timeout 120 apt-get update -qq 2>&1 | tail -5; then
        log_warn "apt-get update had issues, continuing anyway..."
    fi
    
    # Install with timeout (3 minutes max)
    log_info "Installing haproxy package (timeout: 180s)..."
    if timeout 180 apt-get install -y haproxy 2>&1 | tail -10; then
        log_success "HAProxy installed"
        return 0
    else
        log_error "Failed to install HAProxy (timeout or error)"
        return 1
    fi
}

migrate_haproxy_config_from_volume() {
    # Check for config in Docker volume (old container setup)
    local volume_config="/var/lib/docker/volumes/monitoring-node_haproxy_config/_data/haproxy.cfg"
    local host_config="/etc/haproxy/haproxy.cfg"
    
    if [ -f "$volume_config" ]; then
        log_info "Found HAProxy config in Docker volume"
        
        # Check if volume config has actual rules (not just defaults)
        if grep -q "RULES START" "$volume_config" && grep -q "frontend\|backend" "$volume_config"; then
            log_info "Migrating config from Docker volume..."
            
            # Backup existing config if present
            if [ -f "$host_config" ]; then
                cp "$host_config" "${host_config}.bak.$(date +%s)"
            fi
            
            # Copy from volume to host
            cp "$volume_config" "$host_config"
            chmod 644 "$host_config"
            log_success "Config migrated from Docker volume"
            return 0
        else
            log_info "Volume config has no rules, skipping migration"
        fi
    fi
    
    return 1
}

migrate_haproxy_container_to_native() {
    log_info "=== HAProxy Migration: Container → Native ==="
    
    # First, try to migrate config from Docker volume BEFORE stopping container
    local config_migrated=0
    if migrate_haproxy_config_from_volume; then
        config_migrated=1
    fi
    
    # Install native HAProxy if not present
    if ! check_native_haproxy; then
        if ! install_native_haproxy; then
            log_error "Failed to install native HAProxy"
            return 1
        fi
    else
        log_success "Native HAProxy already installed"
    fi
    
    # Stop and remove container
    log_info "Stopping HAProxy container..."
    docker stop monitoring-haproxy >/dev/null 2>&1 || true
    docker rm -f monitoring-haproxy >/dev/null 2>&1 || true
    log_success "HAProxy container removed"
    
    # If config wasn't migrated yet, try again (in case volume check failed earlier)
    if [ $config_migrated -eq 0 ]; then
        migrate_haproxy_config_from_volume || true
    fi
    
    # Validate and start HAProxy
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

# Detect best mirrors
log_info "Detecting fastest mirrors..."
detect_best_mirrors

# Enable BuildKit for faster builds with cache
export DOCKER_BUILDKIT=1

# Generate cache bust hash from .env (forces rebuild when any config changes)
CACHE_BUST=""
if [ -f "$NODE_DIR/.env" ]; then
    CACHE_BUST=$(md5sum "$NODE_DIR/.env" | cut -d' ' -f1)
    export CACHE_BUST
    log_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
fi

# Rebuild Docker image with timeout, mirrors, and caching
log_info "Building new Docker image (timeout: ${DOCKER_BUILD_TIMEOUT}s)..."
log_info "BuildKit enabled for faster cached builds"
cd "$NODE_DIR"

# Build arguments with detected mirrors
BUILD_ARGS="--build-arg APT_MIRROR=${BEST_APT_MIRROR:-mirror.yandex.ru}"
BUILD_ARGS="$BUILD_ARGS --build-arg PIP_INDEX_URL=${BEST_PYPI_MIRROR:-https://pypi.org/simple}"
BUILD_ARGS="$BUILD_ARGS --build-arg PIP_TIMEOUT=${PIP_TIMEOUT}"
BUILD_ARGS="$BUILD_ARGS --build-arg APT_TIMEOUT=${APT_TIMEOUT}"
BUILD_ARGS="$BUILD_ARGS --build-arg CACHE_BUST=${CACHE_BUST}"

log_info "Using mirrors: APT=${BEST_APT_MIRROR:-default}, PyPI=${BEST_PYPI_MIRROR:-default}"

# Run build in background, capture output to log file
set +e
timeout "$DOCKER_BUILD_TIMEOUT" docker build --network=host $BUILD_ARGS -t monitoring-node-api . > "$BUILD_LOG" 2>&1 &
BUILD_PID=$!

# Show progress while building
DOTS=""
while kill -0 $BUILD_PID 2>/dev/null; do
    DOTS="${DOTS}."
    if [ ${#DOTS} -gt 3 ]; then DOTS="."; fi
    CURRENT_STEP=$(grep -oE 'Step [0-9]+/[0-9]+|#[0-9]+ \[[0-9]+/[0-9]+\]' "$BUILD_LOG" 2>/dev/null | tail -1)
    if [ -n "$CURRENT_STEP" ]; then
        printf "\r${CYAN}[INFO]${NC} Building${DOTS} %-30s" "($CURRENT_STEP)"
    else
        printf "\r${CYAN}[INFO]${NC} Building${DOTS}   "
    fi
    sleep 2
done
printf "\r%-60s\r" " "

wait $BUILD_PID
BUILD_EXIT_CODE=$?
set -e

if [ $BUILD_EXIT_CODE -eq 0 ]; then
    log_success "Image built"
    rm -f "$BUILD_LOG"
elif [ $BUILD_EXIT_CODE -eq 124 ]; then
    log_error "Build timeout after ${DOCKER_BUILD_TIMEOUT}s"
    echo "Try increasing timeout: export DOCKER_BUILD_TIMEOUT=3600"
    echo "Or check server memory: free -h"
    echo ""
    echo -e "${YELLOW}Last 50 lines of build output:${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
else
    log_error "Build failed (exit code: $BUILD_EXIT_CODE)"
    echo ""
    echo -e "${YELLOW}Last 50 lines of build output:${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
fi

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
