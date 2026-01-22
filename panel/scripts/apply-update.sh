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

# Timeouts (in seconds)
DOCKER_BUILD_TIMEOUT="${DOCKER_BUILD_TIMEOUT:-1800}"  # 30 min default
APT_TIMEOUT="${APT_TIMEOUT:-120}"
PIP_TIMEOUT="${PIP_TIMEOUT:-120}"
NPM_TIMEOUT="${NPM_TIMEOUT:-120000}"  # npm uses milliseconds

# Best mirrors (will be detected)
BEST_PYPI_MIRROR=""
BEST_NPM_MIRROR=""
BEST_APT_MIRROR=""

# Arguments
TMP_DIR="$1"
PANEL_DIR="$2"
CURRENT_VERSION="$3"

if [ -z "$TMP_DIR" ] || [ -z "$PANEL_DIR" ]; then
    log_error "Usage: apply-update.sh <tmp_dir> <panel_dir> [current_version]"
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

detect_best_npm_mirror() {
    local best_mirror="https://registry.npmjs.org"
    local best_time=9999
    local test_urls=(
        "https://registry.npmjs.org/npm"
        "https://registry.npmmirror.com/npm"
    )
    local mirrors=(
        "https://registry.npmjs.org"
        "https://registry.npmmirror.com"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || time_ms=9999
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_NPM_MIRROR="$best_mirror"
    [ "$best_time" -lt 9999 ] && log_success "Best npm: $best_mirror (${best_time}ms)"
}

detect_best_apt_mirror() {
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
    log_info "Detecting fastest mirrors..."
    detect_best_pypi_mirror
    detect_best_npm_mirror
    detect_best_apt_mirror
}

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

# Clean up Docker before build
log_info "Cleaning up Docker cache..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -af > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Detect best mirrors
detect_best_mirrors

# Rebuild Docker images with timeout and mirrors
log_info "Building new Docker images (timeout: ${DOCKER_BUILD_TIMEOUT}s)..."
cd "$PANEL_DIR"

# Build arguments with detected mirrors
BUILD_ARGS="--build-arg APT_MIRROR=${BEST_APT_MIRROR:-mirror.yandex.ru}"
BUILD_ARGS="$BUILD_ARGS --build-arg PIP_INDEX_URL=${BEST_PYPI_MIRROR:-https://pypi.org/simple}"
BUILD_ARGS="$BUILD_ARGS --build-arg NPM_REGISTRY=${BEST_NPM_MIRROR:-https://registry.npmmirror.com}"
BUILD_ARGS="$BUILD_ARGS --build-arg PIP_TIMEOUT=${PIP_TIMEOUT}"
BUILD_ARGS="$BUILD_ARGS --build-arg APT_TIMEOUT=${APT_TIMEOUT}"
BUILD_ARGS="$BUILD_ARGS --build-arg NPM_TIMEOUT=${NPM_TIMEOUT}"

log_info "Using mirrors: APT=${BEST_APT_MIRROR:-default}, PyPI=${BEST_PYPI_MIRROR:-default}, npm=${BEST_NPM_MIRROR:-default}"

BUILD_OUTPUT=$(timeout "$DOCKER_BUILD_TIMEOUT" docker compose build --no-cache $BUILD_ARGS 2>&1)
BUILD_EXIT_CODE=$?

if [ $BUILD_EXIT_CODE -eq 0 ]; then
    log_success "Images built"
elif [ $BUILD_EXIT_CODE -eq 124 ]; then
    log_error "Build timeout after ${DOCKER_BUILD_TIMEOUT}s"
    echo "Try increasing timeout: export DOCKER_BUILD_TIMEOUT=3600"
    exit 1
else
    log_error "Build failed (exit code: $BUILD_EXIT_CODE)"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo "$BUILD_OUTPUT" | tail -30
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
