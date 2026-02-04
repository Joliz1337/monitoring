#!/bin/bash
#
# Panel Update Script - Downloads update and runs fresh updater
#
# Usage: ./update.sh [commit_hash|tag|branch]
#   If no argument provided, updates to latest commit from main branch
#

# ==================== Safety Settings ====================

set +e  # Handle errors manually

LOCKFILE="/tmp/monitoring-panel-update.lock"
LOCK_FD=200

# ==================== Timeouts Configuration ====================

TIMEOUT_GIT_CLONE=180
TIMEOUT_DOCKER_COMPOSE_DOWN=120
TIMEOUT_DOCKER_BUILD="${DOCKER_BUILD_TIMEOUT:-1200}"
TIMEOUT_CONNECTIVITY_CHECK=15

MAX_RETRIES=3
RETRY_DELAY=5

# ==================== Lock Management ====================

acquire_lock() {
    eval "exec $LOCK_FD>$LOCKFILE"
    if ! flock -n $LOCK_FD 2>/dev/null; then
        echo -e "\033[0;31m[ERROR] Another update is already running\033[0m"
        exit 1
    fi
    echo $$ > "$LOCKFILE"
}

release_lock() {
    flock -u $LOCK_FD 2>/dev/null || true
    rm -f "$LOCKFILE" 2>/dev/null || true
}

# ==================== Cleanup ====================

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    
    release_lock
    
    if [ $exit_code -ne 0 ] && [ $exit_code -ne 130 ] && [ $exit_code -ne 143 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script failed (exit code: $exit_code)\033[0m"
    fi
    
    [ -d "$TMP_DIR" ] && rm -rf "$TMP_DIR" 2>/dev/null || true
    exit $exit_code
}

trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Terminated by signal\033[0m"; exit 143' TERM

# ==================== Colors ====================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Configuration ====================

PANEL_DIR="/opt/monitoring-panel"
TMP_DIR="/tmp/panel-update-$$"
TARGET_REF="${1:-main}"
REPO_URL="https://github.com/Joliz1337/monitoring.git"

# ==================== Safe Execution Helpers ====================

run_timeout_retry() {
    local timeout_sec="$1"
    local max_retries="$2"
    local delay="$3"
    local desc="$4"
    shift 4
    
    local attempt=1
    local output
    local exit_code
    
    while [ $attempt -le $max_retries ]; do
        output=$(timeout "$timeout_sec" "$@" 2>&1)
        exit_code=$?
        
        if [ $exit_code -eq 0 ]; then
            return 0
        fi
        
        if [ $exit_code -eq 124 ]; then
            log_warn "$desc - timeout (${timeout_sec}s, attempt $attempt/$max_retries)"
        else
            log_warn "$desc - failed (exit $exit_code, attempt $attempt/$max_retries)"
        fi
        
        if [ $attempt -lt $max_retries ]; then
            sleep "$delay"
        fi
        
        attempt=$((attempt + 1))
    done
    
    log_error "$desc - failed after $max_retries attempts"
    return 1
}

# ==================== Network Functions ====================

check_connectivity_quiet() {
    local test_urls=(
        "https://github.com"
        "https://1.1.1.1"
        "https://8.8.8.8"
    )
    
    for url in "${test_urls[@]}"; do
        if timeout "$TIMEOUT_CONNECTIVITY_CHECK" curl -fsSL --connect-timeout 5 --max-time 10 "$url" >/dev/null 2>&1; then
            return 0
        fi
    done
    
    return 1
}

# ==================== GitHub Clone Functions ====================

clone_repo() {
    local target_dir="$1"
    local branch="$2"
    local max_retries=$MAX_RETRIES
    local retry=0
    
    while [ $retry -lt $max_retries ]; do
        rm -rf "$target_dir" 2>/dev/null || true
        
        log_info "Downloading from GitHub (attempt $((retry + 1))/$max_retries)..."
        
        if timeout "$TIMEOUT_GIT_CLONE" git clone --depth 1 --branch "$branch" "$REPO_URL" "$target_dir" 2>&1; then
            log_success "Download complete"
            return 0
        fi
        
        retry=$((retry + 1))
        if [ $retry -lt $max_retries ]; then
            log_warn "Download failed, retrying in ${RETRY_DELAY}s..."
            sleep "$RETRY_DELAY"
            
            if ! check_connectivity_quiet; then
                log_warn "Network connectivity issue detected"
            fi
        fi
    done
    
    log_error "Failed to download after $max_retries attempts"
    return 1
}

# ==================== Validation Functions ====================

check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker not found"
        return 1
    fi
    
    if ! timeout 10 docker info &> /dev/null; then
        log_error "Cannot connect to Docker daemon"
        return 1
    fi
    
    return 0
}

check_installation() {
    if [ ! -f "$PANEL_DIR/docker-compose.yml" ]; then
        log_error "Panel installation not found at $PANEL_DIR"
        return 1
    fi
    return 0
}

# ==================== Backup Functions ====================

backup_config() {
    log_info "Backing up configuration..."
    
    if [ -f "$PANEL_DIR/.env" ]; then
        if cp "$PANEL_DIR/.env" "$PANEL_DIR/.env.backup" 2>/dev/null; then
            log_success ".env backed up"
        else
            log_warn "Could not backup .env"
        fi
    fi
    
    if [ -f "$PANEL_DIR/backend/.env" ]; then
        if cp "$PANEL_DIR/backend/.env" "$PANEL_DIR/backend/.env.backup" 2>/dev/null; then
            log_success "backend/.env backed up"
        else
            log_warn "Could not backup backend/.env"
        fi
    fi
}

restore_config() {
    if [ -f "$PANEL_DIR/.env.backup" ]; then
        mv "$PANEL_DIR/.env.backup" "$PANEL_DIR/.env" 2>/dev/null || true
    fi
    
    if [ -f "$PANEL_DIR/backend/.env.backup" ]; then
        mv "$PANEL_DIR/backend/.env.backup" "$PANEL_DIR/backend/.env" 2>/dev/null || true
    fi
}

# ==================== Fallback Update ====================

fallback_update() {
    local new_version="unknown"
    if [ -f "$TMP_DIR/panel/VERSION" ]; then
        new_version=$(cat "$TMP_DIR/panel/VERSION" 2>/dev/null || echo "unknown")
    fi
    log_info "New version: $new_version"
    
    log_info "Stopping containers..."
    cd "$PANEL_DIR" || return 1
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down --timeout 30 2>/dev/null || true
    log_success "Containers stopped"
    
    log_info "Copying new files..."
    if ! rsync -av --delete \
        --exclude='.env' \
        --exclude='.env.backup' \
        --exclude='backend/.env' \
        --exclude='backend/.env.backup' \
        --exclude='backend/data' \
        --exclude='nginx/nginx.conf' \
        "$TMP_DIR/panel/" "$PANEL_DIR/" 2>/dev/null; then
        log_error "Failed to copy files"
        restore_config
        return 1
    fi
    
    restore_config
    
    if [ -f "$PANEL_DIR/.env" ]; then
        source "$PANEL_DIR/.env" 2>/dev/null || true
        if [ -n "$DOMAIN" ] && [ -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
            export DOMAIN PANEL_UID
            envsubst '${DOMAIN} ${PANEL_UID}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"
        fi
    fi
    
    chmod +x "$PANEL_DIR"/*.sh 2>/dev/null || true
    export DOCKER_BUILDKIT=1
    export COMPOSE_DOCKER_CLI_BUILD=1
    
    if [ -f "$PANEL_DIR/.env" ]; then
        export CACHE_BUST=$(md5sum "$PANEL_DIR/.env" 2>/dev/null | cut -d' ' -f1 || echo "nocache")
    fi
    
    log_info "Building containers..."
    if ! timeout "$TIMEOUT_DOCKER_BUILD" docker compose build 2>&1; then
        log_error "Docker build failed"
        return 1
    fi
    
    log_info "Starting containers..."
    if ! docker compose up -d 2>&1; then
        log_error "Failed to start containers"
        return 1
    fi
    
    log_success "=== Update Complete ==="
    log_info "Version: $CURRENT_VERSION → $new_version"
}

# ==================== Main ====================

main() {
    acquire_lock
    
    log_info "=== Monitoring Panel Update ==="
    log_info "Target: $TARGET_REF"
    log_info "Panel directory: $PANEL_DIR"
    
    check_docker || exit 1
    check_installation || exit 1
    
    CURRENT_VERSION="unknown"
    if [ -f "$PANEL_DIR/VERSION" ]; then
        CURRENT_VERSION=$(cat "$PANEL_DIR/VERSION" 2>/dev/null || echo "unknown")
    fi
    log_info "Current version: $CURRENT_VERSION"
    
    backup_config
    
    if ! clone_repo "$TMP_DIR" "$TARGET_REF"; then
        log_error "Failed to download repository"
        exit 1
    fi
    
    if [ ! -d "$TMP_DIR/panel" ]; then
        log_error "Failed to download repository (panel directory not found)"
        exit 1
    fi
    
    log_info "Running fresh updater from downloaded version..."
    echo ""
    
    if [ -f "$TMP_DIR/panel/scripts/apply-update.sh" ]; then
        chmod +x "$TMP_DIR/panel/scripts/apply-update.sh" 2>/dev/null || true
        exec bash "$TMP_DIR/panel/scripts/apply-update.sh" "$TMP_DIR" "$PANEL_DIR" "$CURRENT_VERSION"
    else
        log_warn "Downloaded version doesn't have apply-update.sh, using inline update..."
        fallback_update
    fi
}

main "$@"
