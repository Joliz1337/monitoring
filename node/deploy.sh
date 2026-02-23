#!/bin/bash
#
# Monitoring Node Agent - Auto Deploy Script
# Supports: Ubuntu 20.04+, Debian 11+
#

# ==================== Safety Settings ====================

set +e  # Handle errors manually

# Prevent interactive prompts during package installation
# needrestart on Ubuntu 22.04+ shows ncurses dialog that hangs scripts
# and can restart sshd, killing the SSH session
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

LOCKFILE="/tmp/monitoring-node-deploy.lock"
LOCK_FD=200

# ==================== Timeouts Configuration ====================

TIMEOUT_USER_INPUT=300
TIMEOUT_APT_UPDATE=120
TIMEOUT_APT_INSTALL=300
TIMEOUT_CURL=60
TIMEOUT_DOCKER_COMPOSE_DOWN=120
TIMEOUT_DOCKER_PULL=300
TIMEOUT_SYSTEMCTL=60
TIMEOUT_HEALTH_CHECK=5

MAX_RETRIES=3
RETRY_DELAY=5

# ==================== Lock Management ====================

acquire_lock() {
    eval "exec $LOCK_FD>$LOCKFILE"
    if ! flock -n $LOCK_FD 2>/dev/null; then
        echo -e "\033[0;31m[ERROR] Another deploy is already running\033[0m"
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
    
    exit $exit_code
}

trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Terminated by signal\033[0m"; exit 143' TERM

# ==================== Colors ====================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Safe Execution Helpers ====================

safe_read() {
    local prompt="$1"
    local default="$2"
    local timeout="${3:-30}"
    local input=""
    
    # Ensure we're reading from terminal
    if [ -t 0 ]; then
        # Explicitly print prompt to /dev/tty to ensure visibility
        printf "%s" "$prompt" >/dev/tty 2>/dev/null || printf "%s" "$prompt"
        if read -t "$timeout" -r input </dev/tty 2>/dev/null; then
            if [ -n "$input" ]; then
                echo "$input"
            else
                echo "$default"
            fi
        else
            # Print newline after timeout
            echo "" >/dev/tty 2>/dev/null || true
            echo "$default"
        fi
    else
        # Non-interactive mode - use default
        echo "$default"
    fi
}

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

safe_write_file() {
    local file="$1"
    local content="$2"
    local backup="${file}.bak.$(date +%Y%m%d_%H%M%S)"
    
    if [ -f "$file" ]; then
        cp "$file" "$backup" 2>/dev/null || true
    fi
    
    mkdir -p "$(dirname "$file")" 2>/dev/null || true
    
    if echo "$content" > "$file" 2>/dev/null; then
        return 0
    else
        if [ -f "$backup" ]; then
            mv "$backup" "$file" 2>/dev/null || true
        fi
        return 1
    fi
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

configure_apt_proxy() {
    if [ -f /etc/apt/apt.conf ]; then
        sed -i '/Acquire::.*::Proxy/d' /etc/apt/apt.conf 2>/dev/null || true
    fi
    for f in /etc/apt/apt.conf.d/*; do
        [ -f "$f" ] || continue
        [ "$(basename "$f")" = "99monitoring-proxy" ] && continue
        if grep -q 'Acquire::.*::Proxy' "$f" 2>/dev/null; then
            sed -i '/Acquire::.*::Proxy/d' "$f" 2>/dev/null || true
        fi
    done

    [ -f /etc/monitoring/proxy.conf ] || return 0
    . /etc/monitoring/proxy.conf 2>/dev/null || return 0
    [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ] || { rm -f /etc/apt/apt.conf.d/99monitoring-proxy 2>/dev/null; return 0; }

    mkdir -p /etc/apt/apt.conf.d 2>/dev/null || true
    cat > /etc/apt/apt.conf.d/99monitoring-proxy << PROXYEOF
Acquire::http::Proxy "$PROXY_URL";
Acquire::https::Proxy "$PROXY_URL";
PROXYEOF
}

configure_docker_proxy() {
    [ -f /etc/monitoring/proxy.conf ] || return 0
    . /etc/monitoring/proxy.conf 2>/dev/null || return 0
    [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ] || return 0
    command -v docker &>/dev/null || return 0

    mkdir -p /etc/systemd/system/docker.service.d 2>/dev/null || true
    cat > /etc/systemd/system/docker.service.d/proxy.conf << PROXYEOF
[Service]
Environment="HTTP_PROXY=$PROXY_URL"
Environment="HTTPS_PROXY=$PROXY_URL"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
PROXYEOF
    timeout 60 systemctl daemon-reload >/dev/null 2>&1 || true
    timeout 60 systemctl restart docker >/dev/null 2>&1 || true
}

# ==================== APT Lock Wait ====================

wait_for_apt_lock() {
    local max_wait=120
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
        if [ $waited -eq 0 ]; then
            log_warn "Waiting for apt lock..."
        fi
        sleep 3
        waited=$((waited + 3))
        if [ $waited -ge $max_wait ]; then
            log_warn "apt lock wait timeout (${max_wait}s), trying anyway..."
            return 0
        fi
    done
    return 0
}

# ==================== System Checks ====================

check_disk_space() {
    local required_mb="${1:-2000}"
    local available_mb
    
    available_mb=$(df -m /opt 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
    
    if [ "$available_mb" -lt "$required_mb" ] 2>/dev/null; then
        log_warn "Low disk space: ${available_mb}MB available, ${required_mb}MB required"
        return 1
    fi
    return 0
}

check_memory() {
    local required_mb="${1:-512}"
    local available_mb
    
    available_mb=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}' || echo "0")
    
    if [ "$available_mb" -lt "$required_mb" ] 2>/dev/null; then
        log_warn "Low memory: ${available_mb}MB available, ${required_mb}MB recommended"
        return 1
    fi
    return 0
}

# ==================== Core Functions ====================

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Please run as root: sudo ./deploy.sh"
        exit 1
    fi
}

check_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        VERSION=$VERSION_ID
        log_info "Detected OS: $PRETTY_NAME"
    else
        log_error "Cannot detect OS"
        exit 1
    fi
}

install_docker() {
    if command -v docker &> /dev/null; then
        local docker_version
        docker_version=$(docker --version 2>/dev/null | cut -d ' ' -f3 | cut -d ',' -f1 || echo "unknown")
        log_success "Docker already installed: $docker_version"
        return 0
    fi

    log_info "Installing Docker..."
    suppress_needrestart
    wait_for_apt_lock

    spin "Removing old Docker packages" \
        env DEBIAN_FRONTEND=noninteractive \
        apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

    spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
        env DEBIAN_FRONTEND=noninteractive \
        apt-get update -qq || log_warn "apt update had issues"

    spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Docker dependencies" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        ca-certificates curl gnupg lsb-release || {
        log_error "Failed to install dependencies"
        return 1
    }

    install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true

    if ! spin "Downloading Docker GPG key" bash -c \
        "curl -fsSL 'https://download.docker.com/linux/$OS/gpg' | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null"; then
        log_error "Failed to download Docker GPG key"
        return 1
    fi
    chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS \
        $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists (Docker repo)" \
        env DEBIAN_FRONTEND=noninteractive \
        apt-get update -qq || log_warn "apt update had issues"

    suppress_needrestart
    spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Docker Engine" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || {
        log_error "Failed to install Docker"
        return 1
    }

    timeout "$TIMEOUT_SYSTEMCTL" systemctl start docker >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl enable docker >/dev/null 2>&1 || true

    log_success "Docker installed successfully"
}

generate_api_key() {
    openssl rand -hex 32 2>/dev/null || cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c 64
}

setup_env() {
    if [ -f .env ]; then
        log_warn ".env already exists, skipping..."
        if grep -q "^PANEL_IP=" .env 2>/dev/null; then
            sed -i "s/^PANEL_IP=.*/PANEL_IP=$PANEL_IP/" .env
        else
            echo "" >> .env
            echo "# Panel IP (set by deploy.sh, used for UFW firewall rule)" >> .env
            echo "PANEL_IP=$PANEL_IP" >> .env
        fi
        return 0
    fi

    log_info "Creating .env from .env.example..."
    cp .env.example .env 2>/dev/null || {
        log_error "Failed to copy .env.example"
        return 1
    }

    local api_key
    api_key=$(generate_api_key)
    sed -i "s/API_KEY=.*/API_KEY=$api_key/" .env

    echo "" >> .env
    echo "# Panel IP (set by deploy.sh, used for UFW firewall rule)" >> .env
    echo "PANEL_IP=$PANEL_IP" >> .env

    log_success "Environment configured"
}

setup_ssl() {
    if [ -f nginx/ssl/cert.pem ] && [ -f nginx/ssl/key.pem ]; then
        log_success "SSL certificate already exists"
        return 0
    fi

    log_info "Generating self-signed SSL certificate..."
    
    mkdir -p nginx/ssl 2>/dev/null || true
    
    if ! openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout nginx/ssl/key.pem \
        -out nginx/ssl/cert.pem \
        -subj "/C=US/ST=State/L=City/O=Monitoring/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null; then
        log_error "Failed to generate SSL certificate"
        return 1
    fi

    chmod 600 nginx/ssl/key.pem 2>/dev/null || true
    chmod 644 nginx/ssl/cert.pem 2>/dev/null || true

    log_success "SSL certificate generated"
}

setup_cert_renewal_cron() {
    log_info "Setting up certificate auto-renewal..."
    
    local script_content='#!/bin/bash
# Auto-renewal script for Let'\''s Encrypt certificates

if ! docker ps -q -f name=monitoring-api | grep -q .; then
    echo "monitoring-api container not running, skipping renewal"
    exit 0
fi

HAPROXY_WAS_RUNNING=false
if systemctl is-active --quiet haproxy; then
    HAPROXY_WAS_RUNNING=true
    systemctl stop haproxy
fi

docker exec monitoring-api certbot renew --non-interactive --quiet

for cert_dir in /etc/letsencrypt/live/*/; do
    if [ -d "$cert_dir" ]; then
        domain=$(basename "$cert_dir")
        if [ -f "$cert_dir/fullchain.pem" ] && [ -f "$cert_dir/privkey.pem" ]; then
            cat "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem" > "$cert_dir/combined.pem"
            chmod 600 "$cert_dir/combined.pem"
        fi
    fi
done

if [ "$HAPROXY_WAS_RUNNING" = true ]; then
    systemctl start haproxy
fi

if systemctl is-active --quiet haproxy; then
    systemctl reload haproxy 2>/dev/null || true
fi'

    mkdir -p /opt/monitoring-node 2>/dev/null || true
    safe_write_file "/opt/monitoring-node/renew-certs.sh" "$script_content"
    chmod +x /opt/monitoring-node/renew-certs.sh 2>/dev/null || true
    
    local cron_content='# Auto-renewal of Let'\''s Encrypt certificates
0 3 * * * root /opt/monitoring-node/renew-certs.sh >> /var/log/certbot-renew.log 2>&1'
    
    safe_write_file "/etc/cron.d/certbot-renew" "$cron_content"
    chmod 644 /etc/cron.d/certbot-renew 2>/dev/null || true
    
    log_success "Certificate auto-renewal cron configured (daily at 3:00 AM)"
}

check_haproxy_status() {
    log_info "Checking HAProxy status..."
    
    if timeout 5 systemctl is-active --quiet haproxy 2>/dev/null; then
        log_success "HAProxy service is running (will not be restarted)"
    elif command -v haproxy &>/dev/null; then
        log_info "HAProxy is installed but not running"
    else
        log_info "HAProxy is not installed"
    fi
}

ensure_haproxy_dir() {
    if [ ! -d "/etc/haproxy" ]; then
        log_info "Creating /etc/haproxy directory..."
        mkdir -p /etc/haproxy 2>/dev/null || true
        chmod 755 /etc/haproxy 2>/dev/null || true
    fi
}

validate_ip() {
    local ip=$1
    if [[ $ip =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        IFS='.' read -ra ADDR <<< "$ip"
        for i in "${ADDR[@]}"; do
            if [ "$i" -gt 255 ] 2>/dev/null; then
                return 1
            fi
        done
        return 0
    fi
    return 1
}

ask_panel_ip() {
    echo ""
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}       Panel IP Configuration${NC}"
    echo -e "${YELLOW}========================================${NC}"
    echo ""
    echo -e "Enter the IP address of your monitoring panel."
    echo -e "Port 9100 will be accessible ONLY from this IP."
    echo ""
    
    local max_attempts=5
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        PANEL_IP=$(safe_read "Panel IP address: " "" 60)
        
        if [ -z "$PANEL_IP" ]; then
            log_error "IP address cannot be empty"
            attempt=$((attempt + 1))
            continue
        fi
        
        if validate_ip "$PANEL_IP"; then
            log_success "IP address validated: $PANEL_IP"
            return 0
        else
            log_error "Invalid IP address format. Please enter a valid IPv4 address."
            attempt=$((attempt + 1))
        fi
    done
    
    log_error "Too many invalid attempts"
    exit 1
}

setup_firewall() {
    log_info "Configuring firewall..."

    if ! command -v ufw &> /dev/null; then
        suppress_needrestart
        wait_for_apt_lock
        spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
            env DEBIAN_FRONTEND=noninteractive \
            apt-get update -qq || true
        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing UFW" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            ufw || log_warn "UFW installation had issues"
    fi

    if ! command -v ipset &> /dev/null; then
        suppress_needrestart
        wait_for_apt_lock
        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing ipset" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            ipset || log_warn "ipset installation had issues"
    else
        log_success "ipset already installed"
    fi
    
    local ufw_was_active=false
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw_was_active=true
    fi
    
    ask_panel_ip
    
    ufw delete allow 9100/tcp >/dev/null 2>&1 || true
    
    log_info "Adding UFW rule: allow port 9100 from $PANEL_IP"
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp comment "Monitoring API from Panel" >/dev/null 2>&1 || \
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp >/dev/null 2>&1 || true
    
    ufw allow 80/tcp comment "HTTP for Let's Encrypt" >/dev/null 2>&1 || true
    ufw allow ssh >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
    
    if [ "$ufw_was_active" = true ]; then
        log_success "Firewall configured"
    else
        log_warn "UFW is not active - rules added but firewall remains disabled"
    fi
    
    log_info "Port 9100 accessible only from: $PANEL_IP"
}

pull_and_start() {
    log_info "Building and starting containers..."

    spin "Stopping old containers" \
        timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down 2>/dev/null || true

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

    ensure_haproxy_dir

    spin "Starting containers" docker compose up -d || {
        log_error "Failed to start containers"
        exit 1
    }

    # Cleanup dangling images and build cache from previous versions
    docker image prune -f >/dev/null 2>&1 || true
    docker builder prune -f --keep-storage=500MB >/dev/null 2>&1 || true
}

wait_for_services() {
    log_info "Waiting for services to start..."
    
    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if timeout "$TIMEOUT_HEALTH_CHECK" curl -sk https://localhost:9100/health > /dev/null 2>&1; then
            log_success "Services are ready"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    log_warn "Services may still be starting"
    return 0
}

check_endpoints() {
    log_info "Checking API endpoints..."
    
    local api_key
    api_key=$(grep "^API_KEY=" .env 2>/dev/null | cut -d '=' -f2 || echo "")
    local base_url="https://localhost:9100"

    echo ""
    
    echo -n "  /health: "
    local response
    response=$(timeout "$TIMEOUT_HEALTH_CHECK" curl -sk "$base_url/health" 2>/dev/null || echo "")
    if echo "$response" | grep -q '"status":"ok"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    echo -n "  /api/metrics: "
    response=$(timeout "$TIMEOUT_HEALTH_CHECK" curl -sk -H "X-API-Key: $api_key" "$base_url/api/metrics" 2>/dev/null || echo "")
    if echo "$response" | grep -q '"cpu"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    echo -n "  /api/haproxy/status: "
    response=$(timeout "$TIMEOUT_HEALTH_CHECK" curl -sk -H "X-API-Key: $api_key" "$base_url/api/haproxy/status" 2>/dev/null || echo "")
    if echo "$response" | grep -q '"running":true'; then
        echo -e "${GREEN}OK (running)${NC}"
    elif echo "$response" | grep -q '"running":false'; then
        echo -e "${YELLOW}OK (stopped)${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    echo -n "  /api/traffic/current: "
    response=$(timeout "$TIMEOUT_HEALTH_CHECK" curl -sk -H "X-API-Key: $api_key" "$base_url/api/traffic/current" 2>/dev/null || echo "")
    if echo "$response" | grep -q '"interfaces"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    echo ""
}

get_server_ip() {
    local ip=""
    local services=(
        "https://api.ipify.org"
        "https://icanhazip.com"
        "https://ifconfig.me"
        "https://2ip.me/api/ip"
        "https://checkip.amazonaws.com"
        "https://ipinfo.io/ip"
        "https://ident.me"
        "https://ifconfig.co"
        "https://ipecho.net/plain"
        "https://ip.sb"
    )

    for svc in "${services[@]}"; do
        ip=$(timeout 5 curl -4 -fsSL --noproxy '*' --connect-timeout 3 --max-time 5 "$svc" 2>/dev/null | tr -d '[:space:]')
        if [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
            echo "$ip"
            return 0
        fi
    done

    ip=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | awk '{print $2}')
    [ -n "$ip" ] && echo "$ip" && return 0

    ip=$(hostname -I 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    [ -n "$ip" ] && echo "$ip" && return 0

    echo "unknown"
}

show_status() {
    local final_api_key
    final_api_key=$(grep "^API_KEY=" .env 2>/dev/null | cut -d '=' -f2 || echo "unknown")
    local server_ip
    server_ip=$(get_server_ip)
    
    echo ""
    echo "=========================================="
    echo -e "${GREEN}Deployment Complete!${NC}"
    echo "=========================================="
    echo ""
    echo -e "${GREEN}Firewall Configuration:${NC}"
    echo "  - Panel IP (port 9100 allowed): $PANEL_IP"
    echo "  - SSH (port 22): Open for all"
    echo "  - HTTP (port 80): Open for all"
    echo ""
    echo -e "${GREEN}SSL Auto-Renewal:${NC}"
    echo "  - Cron job: $([ -f /etc/cron.d/certbot-renew ] && echo 'Enabled (daily at 3:00 AM)' || echo 'Not configured')"
    echo ""
    
    echo -e "${GREEN}HAProxy (native systemd service):${NC}"
    if timeout 5 systemctl is-active --quiet haproxy 2>/dev/null; then
        echo -e "  - Status: ${GREEN}Running${NC}"
    elif command -v haproxy &>/dev/null; then
        echo -e "  - Status: ${YELLOW}Stopped${NC}"
    else
        echo -e "  - Status: ${YELLOW}Not installed${NC}"
    fi
    echo "  - Config: /etc/haproxy/haproxy.cfg"
    echo ""
    echo "Container status:"
    docker compose ps 2>/dev/null || true
    echo ""
    echo "Commands:"
    echo "  docker compose logs -f          # View API logs"
    echo "  docker compose restart          # Restart API"
    echo "  systemctl status haproxy        # HAProxy status"
    echo ""
    
    echo ""
    echo -e "  ${GREEN}══ CONNECTION DETAILS FOR PANEL ══${NC}"
    echo ""
    echo -e "    ${YELLOW}Server IP:${NC}  ${CYAN}${server_ip}${NC}"
    echo -e "    ${YELLOW}Port:${NC}       ${CYAN}9100${NC}"
    echo ""
    echo -e "    ${YELLOW}API Key:${NC}"
    echo -e "    ${BLUE}${final_api_key}${NC}"
    echo ""
    
    safe_read "Press Enter to finish..." "" 7200 >/dev/null
}

# ==================== Main ====================

main() {
    acquire_lock
    
    echo ""
    echo "=========================================="
    echo " Monitoring Node Agent - Deploy Script"
    echo "=========================================="
    echo ""

    check_root
    check_os
    load_proxy
    configure_apt_proxy
    check_disk_space 2000 || true
    check_memory 512 || true
    check_haproxy_status
    install_docker || exit 1
    configure_docker_proxy
    setup_firewall
    setup_env || exit 1
    setup_ssl || exit 1
    setup_cert_renewal_cron
    pull_and_start
    wait_for_services
    check_endpoints
    show_status
}

cd "$(dirname "$0")" || exit 1
main "$@"
