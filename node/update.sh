#!/bin/bash
#
# Node Update Script - Downloads update and runs fresh updater
#
# Usage: ./update.sh [commit_hash|tag|branch]
#   If no argument provided, updates to latest commit from main branch
#
# Environment variables:
#   UPDATE_PROXY - SOCKS5 proxy for downloading (e.g., socks5h://127.0.0.1:1080)
#

set -e

# Cleanup proxy on exit
cleanup_proxy() {
    # Unset git proxy (only if we set it)
    if [ -n "$SOCKS5_PROXY" ]; then
        git config --global --unset http.proxy 2>/dev/null || true
        git config --global --unset https.proxy 2>/dev/null || true
    fi
}

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    
    # Cleanup proxy settings
    cleanup_proxy
    
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
REPO_URL="https://github.com/Joliz1337/monitoring.git"

# Proxy from environment variable
SOCKS5_PROXY="${UPDATE_PROXY:-}"

# ==================== Proxy Functions ====================

# Enable proxy for all network operations
enable_system_proxy() {
    if [ -z "$SOCKS5_PROXY" ]; then
        return 0
    fi
    
    log_info "Enabling proxy: $SOCKS5_PROXY"
    
    # Set environment variables for all tools
    export http_proxy="$SOCKS5_PROXY"
    export https_proxy="$SOCKS5_PROXY"
    export HTTP_PROXY="$SOCKS5_PROXY"
    export HTTPS_PROXY="$SOCKS5_PROXY"
    export ALL_PROXY="$SOCKS5_PROXY"
    export all_proxy="$SOCKS5_PROXY"
    
    # Git proxy config
    git config --global http.proxy "$SOCKS5_PROXY" 2>/dev/null || true
    git config --global https.proxy "$SOCKS5_PROXY" 2>/dev/null || true
}

# Get git proxy config
get_git_proxy_args() {
    if [ -n "$SOCKS5_PROXY" ]; then
        echo "-c http.proxy=$SOCKS5_PROXY -c https.proxy=$SOCKS5_PROXY"
    fi
}

# ==================== GitHub Clone Functions ====================

clone_repo() {
    local target_dir="$1"
    local branch="$2"
    local max_retries=3
    local retry=0
    local git_proxy_args=$(get_git_proxy_args)
    
    while [ $retry -lt $max_retries ]; do
        rm -rf "$target_dir"
        
        log_info "Downloading from GitHub (attempt $((retry + 1))/$max_retries)..."
        if [ -n "$git_proxy_args" ]; then
            log_info "Using proxy: $SOCKS5_PROXY"
            if timeout 120 git $git_proxy_args clone --depth 1 --branch "$branch" "$REPO_URL" "$target_dir" 2>&1; then
                log_success "Download complete"
                return 0
            fi
        else
            if timeout 120 git clone --depth 1 --branch "$branch" "$REPO_URL" "$target_dir" 2>&1; then
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

# Enable proxy if provided
enable_system_proxy

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
if ! clone_repo "$TMP_DIR" "$TARGET_REF"; then
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
    # Pass proxy via environment variable
    UPDATE_PROXY="$SOCKS5_PROXY" exec bash "$TMP_DIR/node/scripts/apply-update.sh" "$TMP_DIR" "$NODE_DIR" "$CURRENT_VERSION"
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
