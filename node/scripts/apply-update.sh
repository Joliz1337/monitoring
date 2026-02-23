#!/bin/bash
#
# Apply Update Script - called by update.sh after downloading new version
# This ensures all update logic uses the LATEST code
#

set -e

# Prevent interactive prompts during package installation
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
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

# ==================== Container Detection ====================

IN_CONTAINER=0
if [ -f /.dockerenv ] || grep -qE 'docker|lxc|containerd' /proc/1/cgroup 2>/dev/null; then
    IN_CONTAINER=1
    log_warn "Running inside container — host package management (apt-get, systemctl) will be skipped"
fi

# ==================== Progress Spinner ====================

# Run command with animated spinner showing elapsed time
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

suppress_needrestart() {
    if [ -d /etc/needrestart ] || dpkg -l needrestart &>/dev/null 2>&1; then
        mkdir -p /etc/needrestart/conf.d 2>/dev/null || true
        echo '$nrconf{restart} = "l";' > /etc/needrestart/conf.d/no-prompt.conf 2>/dev/null || true
    fi
    pkill -9 needrestart 2>/dev/null || true
}

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
load_proxy

# ==================== Configuration ====================

# Timeouts (in seconds)
DOCKER_PULL_TIMEOUT="${DOCKER_PULL_TIMEOUT:-300}"

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
    suppress_needrestart

    if ! wait_for_apt_lock; then
        log_error "Cannot acquire apt lock"
        return 1
    fi

    spin_retry 120 3 5 "Updating package lists" \
        env DEBIAN_FRONTEND=noninteractive \
        apt-get update -qq || log_warn "apt-get update had issues, continuing anyway..."

    suppress_needrestart
    if spin_retry 180 3 5 "Installing HAProxy" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        haproxy; then
        return 0
    else
        log_error "Failed to install HAProxy"
        return 1
    fi
}

install_ipset() {
    if command -v ipset &>/dev/null; then
        log_success "ipset already installed"
        return 0
    fi

    suppress_needrestart

    if ! wait_for_apt_lock; then
        log_error "Cannot acquire apt lock"
        return 1
    fi

    if spin_retry 120 3 5 "Installing ipset" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        ipset; then
        return 0
    else
        log_warn "Failed to install ipset — IP blocklist may not work"
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

if [ $IN_CONTAINER -eq 0 ]; then
    if check_haproxy_container; then
        log_warn "Detected old HAProxy container - migrating to native service"
        migrate_haproxy_container_to_native
    else
        ensure_native_haproxy
    fi

    install_ipset
else
    log_info "Skipping HAProxy/ipset management (not on host)"
    # Still remove old HAProxy container if exists (Docker socket is available)
    if check_haproxy_container; then
        log_info "Removing old HAProxy container..."
        docker stop monitoring-haproxy >/dev/null 2>&1 || true
        docker rm -f monitoring-haproxy >/dev/null 2>&1 || true
    fi
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
    --exclude='configs/' \
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

# Migrate optimization files from old /opt/monitoring-node/ to /opt/monitoring/
if [ -f "/opt/monitoring-node/scripts/network-tune.sh" ] && [ ! -f "/opt/monitoring/scripts/network-tune.sh" ]; then
    mkdir -p /opt/monitoring/scripts 2>/dev/null || true
    mv "/opt/monitoring-node/scripts/network-tune.sh" "/opt/monitoring/scripts/network-tune.sh" 2>/dev/null || true
    rmdir "/opt/monitoring-node/scripts" 2>/dev/null || true
    log_success "Migrated network-tune.sh to /opt/monitoring/scripts/"
fi
if [ -f "/opt/monitoring-node/configs/VERSION" ] && [ ! -f "/opt/monitoring/configs/VERSION" ]; then
    mkdir -p /opt/monitoring/configs 2>/dev/null || true
    mv "/opt/monitoring-node/configs/VERSION" "/opt/monitoring/configs/VERSION" 2>/dev/null || true
    rmdir "/opt/monitoring-node/configs" 2>/dev/null || true
    log_success "Migrated configs/VERSION to /opt/monitoring/configs/"
fi
# Update network-tune.service if it references old path
if [ -f "/etc/systemd/system/network-tune.service" ]; then
    if grep -q "/opt/monitoring-node/scripts/" /etc/systemd/system/network-tune.service 2>/dev/null; then
        sed -i 's|/opt/monitoring-node/scripts/|/opt/monitoring/scripts/|g' /etc/systemd/system/network-tune.service 2>/dev/null || true
        systemctl daemon-reload >/dev/null 2>&1 || true
        log_success "Updated network-tune.service path"
    fi
fi

# Pull new Docker images
cd "$NODE_DIR"

set +e
# Pull ready images from GHCR (normal flow)
if ! spin_retry 120 2 10 "Pulling Docker images" docker compose pull 2>/dev/null; then
    log_warn "Failed to pull from registry, building locally..."
    spin "Pulling base images" bash -c \
        'docker compose pull --ignore-buildable 2>/dev/null || true'
    spin_retry 600 2 10 "Building images from source" docker compose build || {
        log_error "Failed to build images"
        exit 1
    }
fi
set -e

# Ensure /etc/haproxy directory exists (host filesystem only)
if [ $IN_CONTAINER -eq 0 ]; then
    ensure_haproxy_dir
fi

# Start containers
spin "Starting containers" docker compose up -d || {
    log_error "Failed to start containers"
    exit 1
}

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

# Cleanup old Docker images and build cache
log_info "Cleaning up old Docker images..."
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f --keep-storage=500MB >/dev/null 2>&1 || true
log_success "Docker cleanup done"

# Final version
FINAL_VERSION="unknown"
if [ -f "$NODE_DIR/VERSION" ]; then
    FINAL_VERSION=$(cat "$NODE_DIR/VERSION")
fi

log_success "=== Update Complete ==="
log_info "Version: ${CURRENT_VERSION:-unknown} → $FINAL_VERSION"
log_info "Preserved: .env, nginx/ssl, configs/, /etc/haproxy config, /etc/letsencrypt certs, traffic data"

# Show HAProxy status
if [ $IN_CONTAINER -eq 0 ]; then
    if systemctl is-active --quiet haproxy 2>/dev/null; then
        log_success "HAProxy (native service): Running"
    elif command -v haproxy &>/dev/null; then
        log_warn "HAProxy (native service): Installed but not running"
        log_info "Start with: systemctl start haproxy"
    else
        log_warn "HAProxy: Not installed"
        log_info "Install with: apt install haproxy"
    fi
else
    log_info "HAProxy status check skipped (not on host)"
fi
