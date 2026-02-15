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
        apt-get install -y -qq -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" \
        ca-certificates curl gnupg lsb-release || {
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
        apt-get install -y -qq -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" \
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
        log_info "Installing UFW..."
        run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
            apt-get update -qq || true
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing ufw" \
            apt-get install -y -qq -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" \
            ufw || log_warn "UFW installation had issues"
    fi
    
    if ! command -v ipset &> /dev/null; then
        log_info "Installing ipset..."
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing ipset" \
            apt-get install -y -qq -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" \
            ipset || log_warn "ipset installation had issues"
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

pull_and_start() {
    log_info "Pulling and starting containers..."
    
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down >/dev/null 2>&1 || true
    
    local attempt=1
    local pull_success=false
    
    while [ $attempt -le $MAX_RETRIES ]; do
        log_info "Pulling images (attempt $attempt/$MAX_RETRIES)..."
        
        if timeout "$TIMEOUT_DOCKER_PULL" docker compose pull 2>&1; then
            pull_success=true
            log_success "Images pulled successfully"
            break
        fi
        
        log_warn "Pull failed (attempt $attempt/$MAX_RETRIES)"
        attempt=$((attempt + 1))
        
        if [ $attempt -le $MAX_RETRIES ]; then
            sleep "$RETRY_DELAY"
        fi
    done
    
    if [ "$pull_success" = false ]; then
        log_error "Failed to pull images after $MAX_RETRIES attempts"
        echo ""
        echo "Possible solutions:"
        echo "  1. Check if server has internet access"
        echo "  2. Check if images exist in the registry"
        echo "  3. Try: docker compose pull --no-parallel"
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
    
    safe_read "Press Enter to finish..." "" 30 >/dev/null
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
    pull_and_start
    wait_for_services
    check_endpoints
    show_status
}

cd "$(dirname "$0")" || exit 1
main "$@"
