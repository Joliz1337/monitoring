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
TIMEOUT_DOCKER_PULL=300
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

# ==================== Proxy Support ====================

load_proxy() {
    local conf="/etc/monitoring/proxy.conf"
    [ -f "$conf" ] || return 0
    . "$conf" 2>/dev/null || return 0
    [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ] || return 0
    export http_proxy="$PROXY_URL" https_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL" HTTPS_PROXY="$PROXY_URL"
    export all_proxy="$PROXY_URL" ALL_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"
    git config --global http.proxy "$PROXY_URL" 2>/dev/null || true
    git config --global https.proxy "$PROXY_URL" 2>/dev/null || true
}

# ==================== Configuration ====================

PANEL_DIR="/opt/monitoring-panel"
TMP_DIR="/tmp/panel-update-$$"
TARGET_REF="${1:-main}"
REPO_URL="https://github.com/Joliz1337/monitoring.git"

# ==================== Progress Spinner ====================

spin() {
    local desc="$1"; shift
    local logf
    logf=$(mktemp /tmp/.spin-XXXXXX 2>/dev/null || echo "/tmp/.spin-$$")
    local chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local t0
    t0=$(date +%s)

    "$@" >"$logf" 2>&1 &
    local pid=$!
    local i=0

    while kill -0 "$pid" 2>/dev/null; do
        local e=$(( $(date +%s) - t0 ))
        local m=$((e / 60)) s=$((e % 60))
        if [ $m -gt 0 ]; then
            printf "\r  \033[0;36m%s\033[0m %s \033[1;33m[%dm %02ds]\033[0m  " \
                "${chars:$((i % 10)):1}" "$desc" "$m" "$s"
        else
            printf "\r  \033[0;36m%s\033[0m %s \033[1;33m[%ds]\033[0m  " \
                "${chars:$((i % 10)):1}" "$desc" "$s"
        fi
        i=$((i + 1))
        sleep 0.12 2>/dev/null || sleep 1
    done

    wait "$pid" 2>/dev/null
    local rc=$?
    local e=$(( $(date +%s) - t0 ))
    printf "\r\033[2K"

    if [ $rc -eq 0 ]; then
        echo -e "  ${GREEN}✓${NC} ${desc} ${CYAN}(${e}s)${NC}"
    else
        echo -e "  ${RED}✗${NC} ${desc} ${RED}— failed after ${e}s${NC}"
        if [ -s "$logf" ]; then
            echo -e "    ${RED}┌── last output ──────────────────────────${NC}"
            tail -15 "$logf" | while IFS= read -r line; do
                echo -e "    ${RED}│${NC} $line"
            done
            echo -e "    ${RED}└─────────────────────────────────────────${NC}"
        fi
    fi

    rm -f "$logf" 2>/dev/null
    return $rc
}

spin_retry() {
    local tmo="$1" retries="$2" delay="$3" desc="$4"
    shift 4

    local attempt=1
    while [ $attempt -le $retries ]; do
        local label="$desc"
        [ "$retries" -gt 1 ] && label="$desc ($attempt/$retries)"

        if spin "$label" timeout "$tmo" "$@"; then
            return 0
        fi

        [ $attempt -lt $retries ] && sleep "$delay"
        attempt=$((attempt + 1))
    done

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

    rm -rf "$target_dir" 2>/dev/null || true

    if spin_retry "$TIMEOUT_GIT_CLONE" "$MAX_RETRIES" "$RETRY_DELAY" "Downloading from GitHub" \
        git clone --depth 1 --branch "$branch" "$REPO_URL" "$target_dir"; then
        return 0
    fi

    if ! check_connectivity_quiet; then
        log_warn "Network connectivity issue detected"
    fi

    log_error "Failed to download after $MAX_RETRIES attempts"
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
    
    cd "$PANEL_DIR" || return 1
    spin "Stopping containers" \
        timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down --timeout 30 2>/dev/null || true

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

    # Pull ready images from GHCR (normal flow)
    if ! spin_retry 120 2 10 "Pulling Docker images" docker compose pull 2>/dev/null; then
        log_warn "Failed to pull from registry, building locally..."
        spin "Pulling base images" bash -c \
            'docker compose pull --ignore-buildable 2>/dev/null || true'
        spin_retry 600 2 10 "Building images from source" docker compose build || {
            log_error "Failed to build images"
            return 1
        }
    fi

    spin "Starting containers" docker compose up -d || {
        log_error "Failed to start containers"
        return 1
    }

    log_success "=== Update Complete ==="
    log_info "Version: $CURRENT_VERSION → $new_version"
}

# ==================== Main ====================

main() {
    acquire_lock
    load_proxy
    
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
