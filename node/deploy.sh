#!/bin/bash
#
# Monitoring Node Agent - Auto Deploy Script
# Supports: Ubuntu 20.04+, Debian 11+
#

# ==================== Safety Settings ====================

set +e  # Handle errors manually

LOCKFILE="/tmp/monitoring-node-deploy.lock"
LOCK_FD=200
BUILD_LOG="/tmp/docker_build_$$.log"

# ==================== Timeouts Configuration ====================

TIMEOUT_USER_INPUT=300
TIMEOUT_APT_UPDATE=120
TIMEOUT_APT_INSTALL=300
TIMEOUT_CURL=60
TIMEOUT_DOCKER_BUILD="${DOCKER_BUILD_TIMEOUT:-1200}"
TIMEOUT_DOCKER_COMPOSE_DOWN=120
TIMEOUT_SYSTEMCTL=60
TIMEOUT_CONNECTIVITY_CHECK=15
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
        if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
            echo -e "\033[0;31m[ERROR] Last 50 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -50 "$BUILD_LOG" 2>/dev/null || true
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
        fi
    fi
    
    rm -f "$BUILD_LOG" 2>/dev/null || true
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
    local timeout="${3:-$TIMEOUT_USER_INPUT}"
    local result_var="$4"
    local input=""
    
    if read -t "$timeout" -p "$prompt" input 2>/dev/null; then
        if [ -n "$input" ]; then
            eval "$result_var='$input'"
        else
            eval "$result_var='$default'"
        fi
        return 0
    else
        log_warn "Input timeout, using default: $default"
        eval "$result_var='$default'"
        return 0
    fi
}

run_with_retry() {
    local max_retries="$1"
    local delay="$2"
    local desc="$3"
    shift 3
    
    local attempt=1
    local output
    local exit_code
    
    while [ $attempt -le $max_retries ]; do
        output=$("$@" 2>&1)
        exit_code=$?
        
        if [ $exit_code -eq 0 ]; then
            return 0
        fi
        
        if [ $attempt -lt $max_retries ]; then
            log_warn "$desc - failed (attempt $attempt/$max_retries), retrying in ${delay}s..."
            sleep "$delay"
        fi
        
        attempt=$((attempt + 1))
    done
    
    log_error "$desc - failed after $max_retries attempts"
    return $exit_code
}

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

run_quiet() {
    local desc="$1"
    shift
    local output
    local exit_code
    
    output=$("$@" 2>&1)
    exit_code=$?
    
    if [ $exit_code -ne 0 ]; then
        echo ""
        log_error "$desc - failed (exit code: $exit_code)"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo "$output"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        return $exit_code
    fi
    
    return 0
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

# ==================== Network Fix Functions ====================

check_docker_hub_quiet() {
    local urls=(
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull"
        "https://registry-1.docker.io/v2/"
    )
    
    for url in "${urls[@]}"; do
        if timeout "$TIMEOUT_CONNECTIVITY_CHECK" curl -fsSL --connect-timeout 10 --max-time 15 "$url" >/dev/null 2>&1; then
            return 0
        fi
    done
    
    return 1
}

check_docker_hub() {
    log_info "Checking Docker Hub availability..."
    
    if check_docker_hub_quiet; then
        log_success "Docker Hub is accessible"
        return 0
    fi
    
    log_warn "Docker Hub is not accessible"
    return 1
}

disable_ipv6() {
    log_info "Disabling IPv6..."
    
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ] && grep -q "disable_ipv6 = 1" /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null; then
        log_success "IPv6 already disabled"
        sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 || true
        return 0
    fi
    
    local content='net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1'
    
    safe_write_file "/etc/sysctl.d/99-disable-ipv6.conf" "$content"
    
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf >/dev/null 2>&1 || true
    sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
    sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
    sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 || true
    
    log_success "IPv6 disabled"
}

configure_dns() {
    log_info "Configuring DNS..."
    
    if [ -f /etc/resolv.conf ] && [ ! -f /etc/resolv.conf.backup ]; then
        cp /etc/resolv.conf /etc/resolv.conf.backup 2>/dev/null || true
    fi
    
    if [ -L /etc/resolv.conf ] && readlink /etc/resolv.conf 2>/dev/null | grep -q systemd; then
        mkdir -p /etc/systemd/resolved.conf.d 2>/dev/null || true
        local content='[Resolve]
DNS=1.1.1.1 8.8.8.8 1.0.0.1 8.8.4.4
FallbackDNS=9.9.9.9 149.112.112.112'
        safe_write_file "/etc/systemd/resolved.conf.d/dns.conf" "$content"
        timeout "$TIMEOUT_SYSTEMCTL" systemctl restart systemd-resolved >/dev/null 2>&1 || true
    else
        chattr -i /etc/resolv.conf >/dev/null 2>&1 || true
        local content='nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 1.0.0.1
nameserver 8.8.4.4'
        safe_write_file "/etc/resolv.conf" "$content"
    fi
    
    log_success "DNS configured"
}

configure_docker_dns() {
    log_info "Configuring Docker DNS..."
    
    local docker_config_dir="/etc/docker"
    local daemon_json="$docker_config_dir/daemon.json"
    
    mkdir -p "$docker_config_dir" 2>/dev/null || true
    
    if [ -f "$daemon_json" ]; then
        cp "$daemon_json" "${daemon_json}.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        if command -v jq &>/dev/null; then
            jq '. + {"dns": ["1.1.1.1", "8.8.8.8"]}' "$daemon_json" > "${daemon_json}.tmp" 2>/dev/null && \
                mv "${daemon_json}.tmp" "$daemon_json"
        else
            local content='{"dns": ["1.1.1.1", "8.8.8.8"]}'
            safe_write_file "$daemon_json" "$content"
        fi
    else
        local content='{"dns": ["1.1.1.1", "8.8.8.8"]}'
        safe_write_file "$daemon_json" "$content"
    fi
    
    log_success "Docker DNS configured"
}

restart_docker() {
    log_info "Restarting Docker service..."
    
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl restart docker >/dev/null 2>&1 || \
        timeout "$TIMEOUT_SYSTEMCTL" service docker restart >/dev/null 2>&1 || true
    
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if timeout 5 docker info >/dev/null 2>&1; then
            log_success "Docker service restarted"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done
    
    log_warn "Docker may need manual restart"
    return 1
}

fix_docker_network() {
    log_info "Fixing network issues..."
    disable_ipv6
    configure_dns
    configure_docker_dns
    restart_docker
    sleep 3
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

    apt-get remove -y docker docker-engine docker.io containerd runc >/dev/null 2>&1 || true

    run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
        apt-get update -qq || log_warn "apt update had issues"
    
    run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing dependencies" \
        apt-get install -y -qq ca-certificates curl gnupg lsb-release || {
        log_error "Failed to install dependencies"
        return 1
    }

    install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true
    
    if ! timeout "$TIMEOUT_CURL" curl -fsSL "https://download.docker.com/linux/$OS/gpg" 2>/dev/null | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null; then
        log_error "Failed to download Docker GPG key"
        return 1
    fi
    chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS \
        $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
        apt-get update -qq || log_warn "apt update had issues"
    
    run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing docker" \
        apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || {
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
        local input=""
        safe_read "Panel IP address: " "" "$TIMEOUT_USER_INPUT" input
        PANEL_IP="$input"
        
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
        log_info "Installing UFW..."
        run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
            apt-get update -qq || true
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing ufw" \
            apt-get install -y -qq ufw || log_warn "UFW installation had issues"
    fi
    
    if ! command -v ipset &> /dev/null; then
        log_info "Installing ipset..."
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing ipset" \
            apt-get install -y -qq ipset || log_warn "ipset installation had issues"
        log_success "ipset installed"
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

start_containers() {
    log_info "Building and starting containers..."
    
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down >/dev/null 2>&1 || true
    
    export DOCKER_BUILDKIT=1
    
    local max_retries=$MAX_RETRIES
    local retry=0
    local build_success=false
    local build_timeout="$TIMEOUT_DOCKER_BUILD"
    
    while [ $retry -lt $max_retries ]; do
        if ! check_docker_hub_quiet; then
            log_warn "Docker Hub is not accessible"
            if [ $retry -eq 0 ]; then
                fix_docker_network
            fi
        fi
        
        if [ -f .env ]; then
            export CACHE_BUST=$(md5sum .env 2>/dev/null | cut -d' ' -f1 || echo "nocache")
            log_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
        fi
        
        log_info "Building containers (attempt $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        
        local build_exit_code
        
        timeout "$build_timeout" docker build --network=host --build-arg CACHE_BUST=${CACHE_BUST:-} -t monitoring-node-api . > "$BUILD_LOG" 2>&1 &
        local build_pid=$!
        
        while kill -0 $build_pid 2>/dev/null; do
            if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
                clear
                echo -e "${BLUE}[INFO]${NC} Building Docker image (attempt $((retry + 1))/$max_retries)... (press Ctrl+C to cancel)"
                echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                tail -30 "$BUILD_LOG" 2>/dev/null || true
                echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            fi
            sleep 3
        done
        echo ""
        
        wait $build_pid 2>/dev/null
        build_exit_code=$?
        
        if [ $build_exit_code -eq 0 ]; then
            build_success=true
            rm -f "$BUILD_LOG" 2>/dev/null || true
            clear
            echo ""
            echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
            echo -e "${CYAN}║     Monitoring Node Agent - Deploy         ║${NC}"
            echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
            echo ""
            log_success "Docker build completed successfully"
            break
        elif [ $build_exit_code -eq 124 ]; then
            log_error "Build timeout after ${build_timeout}s"
        else
            log_error "Build failed (exit code: $build_exit_code)"
        fi
        
        if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
            echo ""
            echo -e "${YELLOW}Last 30 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -30 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            log_warn "Build failed, retrying after network fix..."
            fix_docker_network
            sleep "$RETRY_DELAY"
        fi
    done
    
    if [ "$build_success" = false ]; then
        log_error "Docker build failed after $max_retries attempts"
        echo ""
        echo "Possible solutions:"
        echo "  1. Check if server has internet access"
        echo "  2. Try using a VPN"
        echo "  3. Check firewall settings"
        echo "  4. Try again later"
        echo "  5. Increase timeout: export DOCKER_BUILD_TIMEOUT=3600"
        echo ""
        exit 1
    fi
    
    ensure_haproxy_dir
    
    log_info "Starting containers..."
    local up_output
    up_output=$(docker compose up -d 2>&1)
    local up_exit_code=$?
    
    if [ $up_exit_code -ne 0 ]; then
        log_error "Failed to start containers (exit code: $up_exit_code)"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo "$up_output"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        exit 1
    fi

    log_success "Containers started"
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

show_status() {
    local final_api_key
    final_api_key=$(grep "^API_KEY=" .env 2>/dev/null | cut -d '=' -f2 || echo "unknown")
    local server_ip
    server_ip=$(timeout 10 curl -s ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "unknown")
    
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
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                       CONNECTION DETAILS FOR PANEL                     ║${NC}"
    echo -e "${GREEN}╠════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}Server IP:${NC}  ${CYAN}${server_ip}${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}Port:${NC}       ${CYAN}9100${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}API Key:${NC}"
    echo -e "${GREEN}║${NC}  ${BLUE}${final_api_key}${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    
    local dummy=""
    safe_read "Press Enter to finish..." "" 60 dummy
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
    check_disk_space 2000 || true
    check_memory 512 || true
    check_haproxy_status
    install_docker || exit 1
    setup_firewall
    setup_env || exit 1
    setup_ssl || exit 1
    setup_cert_renewal_cron
    start_containers
    wait_for_services
    check_endpoints
    show_status
}

cd "$(dirname "$0")" || exit 1
main "$@"
