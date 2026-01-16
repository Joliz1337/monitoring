#!/bin/bash
#
# Panel Update Script - Simple and reliable
# Updates monitoring panel from GitHub repository
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

# GitHub mirrors for Russia
GITHUB_MIRRORS_RU=(
    "https://ghproxy.com/https://github.com"
    "https://mirror.ghproxy.com/https://github.com"
    "https://github.moeyy.xyz/https://github.com"
    "https://gh.ddlc.top/https://github.com"
)

# Server country and active mirror
SERVER_COUNTRY=""
ACTIVE_GITHUB_MIRROR=""

# ==================== Geo Detection & Mirrors ====================

detect_country() {
    log_info "Detecting server location..."
    
    local country=""
    local geo_apis=(
        "http://ip-api.com/json?fields=countryCode"
        "https://ipapi.co/country_code/"
        "https://ipinfo.io/country"
    )
    
    for api in "${geo_apis[@]}"; do
        local response
        response=$(curl -fsSL --connect-timeout 5 --max-time 10 "$api" 2>/dev/null)
        
        if [ -n "$response" ]; then
            if echo "$response" | grep -q "countryCode"; then
                country=$(echo "$response" | grep -o '"countryCode":"[^"]*"' | cut -d'"' -f4)
            else
                country=$(echo "$response" | tr -d '[:space:]' | head -c 2)
            fi
            
            if [ -n "$country" ] && [ ${#country} -eq 2 ]; then
                break
            fi
        fi
    done
    
    if [ -n "$country" ]; then
        SERVER_COUNTRY="$country"
        if [ "$country" = "RU" ]; then
            log_info "Server in Russia - using GitHub mirrors"
        else
            log_info "Server location: $country - using direct GitHub"
        fi
    else
        SERVER_COUNTRY=""
        log_warn "Could not detect location, using direct GitHub"
    fi
}

test_mirror_speed() {
    local mirror_base="$1"
    local test_url
    
    if [ "$mirror_base" = "https://github.com" ]; then
        test_url="https://raw.githubusercontent.com/Joliz1337/monitoring/main/VERSION"
    else
        test_url="${mirror_base}/Joliz1337/monitoring/raw/main/VERSION"
    fi
    
    local result
    result=$(curl -fsSL --connect-timeout 5 --max-time 10 -w "%{speed_download}" -o /dev/null "$test_url" 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        echo "$result" | awk '{printf "%.0f", $1/1024}'
    else
        echo "0"
    fi
}

select_best_mirror() {
    log_info "Testing GitHub mirrors..."
    
    local mirrors=()
    local best_mirror=""
    local best_speed=0
    
    if [ "$SERVER_COUNTRY" = "RU" ]; then
        mirrors=("${GITHUB_MIRRORS_RU[@]}")
    else
        mirrors=("https://github.com")
    fi
    
    for mirror in "${mirrors[@]}"; do
        local display_name
        if [ "$mirror" = "https://github.com" ]; then
            display_name="GitHub (direct)"
        else
            display_name=$(echo "$mirror" | sed 's|https://||' | cut -d'/' -f1)
        fi
        
        echo -n "  Testing $display_name... "
        
        local speed
        speed=$(test_mirror_speed "$mirror")
        
        if [ "$speed" -gt 0 ]; then
            echo -e "${GREEN}${speed} KB/s${NC}"
            if [ "$speed" -gt "$best_speed" ]; then
                best_speed="$speed"
                best_mirror="$mirror"
            fi
        else
            echo -e "${RED}unavailable${NC}"
        fi
    done
    
    if [ -z "$best_mirror" ]; then
        log_warn "All mirrors failed, using direct GitHub"
        best_mirror="https://github.com"
    else
        local display_name
        if [ "$best_mirror" = "https://github.com" ]; then
            display_name="GitHub (direct)"
        else
            display_name=$(echo "$best_mirror" | sed 's|https://||' | cut -d'/' -f1)
        fi
        log_success "Selected: $display_name (${best_speed} KB/s)"
    fi
    
    ACTIVE_GITHUB_MIRROR="$best_mirror"
}

clone_with_mirror() {
    local target_dir="$1"
    local branch="$2"
    
    if [ -z "$ACTIVE_GITHUB_MIRROR" ]; then
        select_best_mirror
    fi
    
    local mirrors=()
    if [ "$SERVER_COUNTRY" = "RU" ]; then
        mirrors=("$ACTIVE_GITHUB_MIRROR" "${GITHUB_MIRRORS_RU[@]}" "https://github.com")
    else
        mirrors=("$ACTIVE_GITHUB_MIRROR" "https://github.com")
    fi
    
    # Remove duplicates
    local unique_mirrors=()
    local seen=""
    for m in "${mirrors[@]}"; do
        if [[ ! " $seen " =~ " $m " ]]; then
            unique_mirrors+=("$m")
            seen="$seen $m"
        fi
    done
    
    for mirror in "${unique_mirrors[@]}"; do
        local repo_url
        if [ "$mirror" = "https://github.com" ]; then
            repo_url="https://github.com/Joliz1337/monitoring.git"
        else
            repo_url="${mirror}/Joliz1337/monitoring.git"
        fi
        
        local display_name
        if [ "$mirror" = "https://github.com" ]; then
            display_name="GitHub (direct)"
        else
            display_name=$(echo "$mirror" | sed 's|https://||' | cut -d'/' -f1)
        fi
        
        log_info "Downloading from $display_name..."
        rm -rf "$target_dir"
        
        if timeout 120 git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir" 2>&1; then
            log_success "Download complete"
            return 0
        fi
        
        log_warn "Download failed, trying next mirror..."
    done
    
    return 1
}

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

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

# Detect country and select best mirror
detect_country
select_best_mirror
echo ""

# Clone repository with mirror fallback
if ! clone_with_mirror "$TMP_DIR" "$TARGET_REF"; then
    log_error "Failed to download repository from all mirrors"
    exit 1
fi

# Check if download succeeded
if [ ! -d "$TMP_DIR/panel" ]; then
    log_error "Failed to download repository"
    exit 1
fi

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

# VERSION file is already copied with rsync from panel/VERSION

# Restore .env files
if [ -f "$PANEL_DIR/.env.backup" ]; then
    mv "$PANEL_DIR/.env.backup" "$PANEL_DIR/.env"
    log_success ".env restored"
fi

if [ -f "$PANEL_DIR/backend/.env.backup" ]; then
    mv "$PANEL_DIR/backend/.env.backup" "$PANEL_DIR/backend/.env"
    log_success "backend/.env restored"
fi

# Regenerate nginx config from template if domain is set
if [ -f "$PANEL_DIR/.env" ]; then
    source "$PANEL_DIR/.env"
    if [ -n "$DOMAIN" ] && [ -f "$PANEL_DIR/nginx/nginx.conf.template" ]; then
        export DOMAIN
        envsubst '${DOMAIN}' < "$PANEL_DIR/nginx/nginx.conf.template" > "$PANEL_DIR/nginx/nginx.conf"
        log_success "Regenerated nginx.conf for $DOMAIN"
    fi
fi

# Make scripts executable
chmod +x "$PANEL_DIR"/*.sh 2>/dev/null || true

log_success "Files updated"

# Clean up Docker before build to free space
log_info "Cleaning up Docker cache..."
docker image prune -f > /dev/null 2>&1 || true
docker builder prune -af > /dev/null 2>&1 || true
log_success "Docker cleanup done"

# Rebuild Docker images
log_info "Building new Docker images..."
cd "$PANEL_DIR"
docker compose build --no-cache
log_success "Images built"

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

# Final version check
FINAL_VERSION="unknown"
if [ -f "$PANEL_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$PANEL_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: $CURRENT_VERSION → $FINAL_VERSION"
log_info "Preserved: .env, database, server list, SSL config"
