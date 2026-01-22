#!/bin/bash
#
# Node Update Script - Downloads update and runs fresh updater
#
# Usage: ./update.sh [commit_hash|tag|branch]
#   If no argument provided, updates to latest commit from main branch
#

set -e

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
    fi
    # Cleanup temp dir
    [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR"
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

NODE_DIR="/opt/monitoring-node"
TMP_DIR="/tmp/node-update-$$"
TARGET_REF="${1:-main}"

# GitHub mirror domain (change this to use different mirror)
GITHUB_MIRROR_DOMAIN="ghfast.top"
GITHUB_MIRROR="https://${GITHUB_MIRROR_DOMAIN}"

# Timeouts
GIT_TIMEOUT="${GIT_TIMEOUT:-60}"
GIT_MIRROR_TIMEOUT="${GIT_MIRROR_TIMEOUT:-180}"

# Best GitHub source (will be detected)
BEST_GITHUB_URL=""

# ==================== GitHub Source Detection ====================

test_github_speed() {
    local url="$1"
    local start_time end_time elapsed
    
    start_time=$(date +%s%N 2>/dev/null || date +%s)
    if timeout 10 git ls-remote --exit-code "$url" HEAD >/dev/null 2>&1; then
        end_time=$(date +%s%N 2>/dev/null || date +%s)
        if [[ "$start_time" =~ ^[0-9]{10,}$ ]]; then
            elapsed=$(( (end_time - start_time) / 1000000 ))
        else
            elapsed=$(( (end_time - start_time) * 1000 ))
        fi
        echo "$elapsed"
        return 0
    fi
    echo "9999"
    return 1
}

detect_best_github_source() {
    log_info "Testing GitHub sources..."
    local direct_url="https://github.com/Joliz1337/monitoring.git"
    local mirror_url="${GITHUB_MIRROR}/https://github.com/Joliz1337/monitoring.git"
    
    local direct_time mirror_time
    direct_time=$(test_github_speed "$direct_url") || direct_time=9999
    mirror_time=$(test_github_speed "$mirror_url") || mirror_time=9999
    
    if [ "$direct_time" -lt 9999 ] && [ "$direct_time" -le "$mirror_time" ]; then
        BEST_GITHUB_URL="$direct_url"
        log_success "Best GitHub source: direct (${direct_time}ms)"
    elif [ "$mirror_time" -lt 9999 ]; then
        BEST_GITHUB_URL="$mirror_url"
        log_success "Best GitHub source: ${GITHUB_MIRROR_DOMAIN} (${mirror_time}ms)"
    else
        BEST_GITHUB_URL="$direct_url"
        log_warn "GitHub sources slow, will try with extended timeout"
    fi
}

# ==================== GitHub Clone Functions ====================

clone_with_fallback() {
    local target_dir="$1"
    local branch="$2"
    local max_retries=3
    local retry=0
    local direct_url="https://github.com/Joliz1337/monitoring.git"
    local mirror_url="${GITHUB_MIRROR}/https://github.com/Joliz1337/monitoring.git"
    
    # Auto-detect best source if not done yet
    if [ -z "$BEST_GITHUB_URL" ]; then
        detect_best_github_source
    fi
    
    while [ $retry -lt $max_retries ]; do
        rm -rf "$target_dir"
        
        # Determine source and timeout based on BEST_GITHUB_URL
        local clone_url="$BEST_GITHUB_URL"
        local clone_timeout="$GIT_TIMEOUT"
        local source_name="GitHub"
        
        if [[ "$clone_url" == *"$GITHUB_MIRROR_DOMAIN"* ]]; then
            source_name="${GITHUB_MIRROR_DOMAIN}"
            clone_timeout="$GIT_MIRROR_TIMEOUT"
        fi
        
        log_info "Downloading from $source_name (attempt $((retry + 1))/$max_retries, timeout: ${clone_timeout}s)..."
        if timeout "$clone_timeout" git clone --depth 1 --branch "$branch" "$clone_url" "$target_dir" 2>&1; then
            log_success "Download complete"
            return 0
        fi
        
        # If best source failed, try the other one
        rm -rf "$target_dir"
        if [[ "$BEST_GITHUB_URL" == *"$GITHUB_MIRROR_DOMAIN"* ]]; then
            log_warn "Mirror failed, trying direct GitHub..."
            if timeout "$GIT_TIMEOUT" git clone --depth 1 --branch "$branch" "$direct_url" "$target_dir" 2>&1; then
                log_success "Download complete"
                return 0
            fi
        else
            log_warn "GitHub timeout/error, trying mirror (${GITHUB_MIRROR_DOMAIN})..."
            if timeout "$GIT_MIRROR_TIMEOUT" git clone --depth 1 --branch "$branch" "$mirror_url" "$target_dir" 2>&1; then
                log_success "Download complete"
                return 0
            fi
        fi
        
        retry=$((retry + 1))
        if [ $retry -lt $max_retries ]; then
            log_warn "Download failed, retrying in 5s..."
            sleep 5
        fi
    done
    
    log_error "Failed to download after $max_retries attempts"
    return 1
}

# ==================== Main ====================

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

# Check download succeeded
if [ ! -d "$TMP_DIR/node" ]; then
    log_error "Failed to download repository"
    exit 1
fi

# Run the FRESH apply-update.sh from downloaded version
log_info "Running fresh updater from downloaded version..."
echo ""

if [ -f "$TMP_DIR/node/scripts/apply-update.sh" ]; then
    chmod +x "$TMP_DIR/node/scripts/apply-update.sh"
    exec bash "$TMP_DIR/node/scripts/apply-update.sh" "$TMP_DIR" "$NODE_DIR" "$CURRENT_VERSION"
else
    # Fallback for older versions without apply-update.sh
    log_warn "Downloaded version doesn't have apply-update.sh, using inline update..."
    
    # Get new version
    NEW_VERSION="unknown"
    if [ -f "$TMP_DIR/node/VERSION" ]; then
        NEW_VERSION=$(cat "$TMP_DIR/node/VERSION")
    fi
    log_info "New version: $NEW_VERSION"
    
    # Stop containers
    log_info "Stopping containers..."
    cd "$NODE_DIR"
    docker compose down --timeout 30 || true
    log_success "Containers stopped"
    
    # Copy files
    log_info "Copying new files..."
    rsync -av --delete \
        --exclude='.env' \
        --exclude='.env.backup' \
        --exclude='nginx/ssl' \
        "$TMP_DIR/node/" "$NODE_DIR/"
    
    # Restore .env
    if [ -f "$NODE_DIR/.env.backup" ]; then
        mv "$NODE_DIR/.env.backup" "$NODE_DIR/.env"
    fi
    
    # Build and start with BuildKit
    chmod +x "$NODE_DIR"/*.sh 2>/dev/null || true
    export DOCKER_BUILDKIT=1
    
    # Generate cache bust hash from .env
    if [ -f "$NODE_DIR/.env" ]; then
        export CACHE_BUST=$(md5sum "$NODE_DIR/.env" | cut -d' ' -f1)
    fi
    
    docker build --network=host --build-arg CACHE_BUST=${CACHE_BUST:-} -t monitoring-node-api .
    docker compose up -d
    
    log_success "=== Update Complete ==="
    log_info "Version: $CURRENT_VERSION → $NEW_VERSION"
fi
