#!/bin/bash
#
# Panel Update Script - Downloads update and runs fresh updater
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

PANEL_DIR="/opt/monitoring-panel"
TMP_DIR="/tmp/panel-update-$$"
TARGET_REF="${1:-main}"

# GitHub mirror for Russia
GITHUB_MIRROR="https://ghfast.top"
ACTIVE_GITHUB_MIRROR=""

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# ==================== GitHub Mirror Functions ====================

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

# ==================== Main ====================

log_info "=== Monitoring Panel Update ==="
log_info "Target: $TARGET_REF"
log_info "Panel directory: $PANEL_DIR"

# Check Docker
if ! command -v docker &> /dev/null; then
    log_error "Docker not found"
    exit 1
fi

if ! docker info &> /dev/null; then
    log_error "Cannot connect to Docker daemon"
    exit 1
fi

# Check panel directory exists
if [ ! -f "$PANEL_DIR/docker-compose.yml" ]; then
    log_error "Panel installation not found at $PANEL_DIR"
    exit 1
fi

# Get current version
CURRENT_VERSION="unknown"
if [ -f "$PANEL_DIR/VERSION" ]; then
    CURRENT_VERSION=$(cat "$PANEL_DIR/VERSION")
fi
log_info "Current version: $CURRENT_VERSION"

# Backup .env files
log_info "Backing up configuration..."
if [ -f "$PANEL_DIR/.env" ]; then
    cp "$PANEL_DIR/.env" "$PANEL_DIR/.env.backup"
    log_success ".env backed up"
fi

if [ -f "$PANEL_DIR/backend/.env" ]; then
    cp "$PANEL_DIR/backend/.env" "$PANEL_DIR/backend/.env.backup"
    log_success "backend/.env backed up"
fi

# Select best mirror
select_best_mirror
echo ""

# Clone repository
if ! clone_with_mirror "$TMP_DIR" "$TARGET_REF"; then
    log_error "Failed to download repository from all mirrors"
    exit 1
fi

# Check download succeeded
if [ ! -d "$TMP_DIR/panel" ]; then
    log_error "Failed to download repository"
    exit 1
fi

# Run the FRESH apply-update.sh from downloaded version
log_info "Running fresh updater from downloaded version..."
echo ""

if [ -f "$TMP_DIR/panel/scripts/apply-update.sh" ]; then
    chmod +x "$TMP_DIR/panel/scripts/apply-update.sh"
    exec bash "$TMP_DIR/panel/scripts/apply-update.sh" "$TMP_DIR" "$PANEL_DIR" "$CURRENT_VERSION"
else
    # Fallback for older versions without apply-update.sh
    log_warn "Downloaded version doesn't have apply-update.sh, using inline update..."
    
    # Get new version
    NEW_VERSION="unknown"
    if [ -f "$TMP_DIR/panel/VERSION" ]; then
        NEW_VERSION=$(cat "$TMP_DIR/panel/VERSION")
    fi
    log_info "New version: $NEW_VERSION"
    
    # Stop containers
    log_info "Stopping containers..."
    cd "$PANEL_DIR"
    docker compose down --timeout 30 || true
    log_success "Containers stopped"
    
    # Copy files
    log_info "Copying new files..."
    rsync -av --delete \
        --exclude='.env' \
        --exclude='.env.backup' \
        --exclude='backend/.env' \
        --exclude='backend/.env.backup' \
        --exclude='backend/data' \
        --exclude='nginx/nginx.conf' \
        "$TMP_DIR/panel/" "$PANEL_DIR/"
    
    # Restore .env
    if [ -f "$PANEL_DIR/.env.backup" ]; then
        mv "$PANEL_DIR/.env.backup" "$PANEL_DIR/.env"
    fi
    
    # Generate nginx config
    if [ -f "$PANEL_DIR/.env" ]; then
        source "$PANEL_DIR/.env"
        if [ -n "$DOMAIN" ] && [ -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
            export DOMAIN PANEL_UID
            envsubst '${DOMAIN} ${PANEL_UID}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"
        fi
    fi
    
    # Build and start
    chmod +x "$PANEL_DIR"/*.sh 2>/dev/null || true
    docker compose build --no-cache
    docker compose up -d
    
    log_success "=== Update Complete ==="
    log_info "Version: $CURRENT_VERSION → $NEW_VERSION"
fi
