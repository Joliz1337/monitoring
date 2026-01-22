#!/bin/bash
#
# Monitoring Node Agent - Auto Deploy Script
# Supports: Ubuntu 20.04+, Debian 11+
#

set -e

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
        echo -e "\033[0;31m[ERROR] Last operation may have failed. Check logs above.\033[0m"
    fi
    exit $exit_code
}
trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[ERROR] Terminated by signal\033[0m"; exit 143' TERM

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Timeouts (in seconds)
DOCKER_BUILD_TIMEOUT="${DOCKER_BUILD_TIMEOUT:-1800}"  # 30 min default
APT_TIMEOUT="${APT_TIMEOUT:-120}"
PIP_TIMEOUT="${PIP_TIMEOUT:-120}"

# Best mirrors (will be detected)
BEST_PYPI_MIRROR=""
BEST_APT_MIRROR=""

# Run command quietly, show full output only on error
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

# ==================== Mirror Speed Testing ====================

test_mirror_speed() {
    local url="$1"
    local timeout_sec="${2:-5}"
    local start_time end_time elapsed
    
    start_time=$(date +%s%N 2>/dev/null || date +%s)
    if curl -fsSL --connect-timeout "$timeout_sec" --max-time "$timeout_sec" "$url" >/dev/null 2>&1; then
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

detect_best_pypi_mirror() {
    log_info "Testing PyPI mirrors..."
    local best_mirror="https://pypi.org/simple"
    local best_time=9999
    local test_urls=(
        "https://pypi.org/simple/pip/"
        "https://pypi.tuna.tsinghua.edu.cn/simple/pip/"
        "https://mirrors.aliyun.com/pypi/simple/pip/"
    )
    local mirrors=(
        "https://pypi.org/simple"
        "https://pypi.tuna.tsinghua.edu.cn/simple"
        "https://mirrors.aliyun.com/pypi/simple"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || true
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_PYPI_MIRROR="$best_mirror"
    if [ "$best_time" -lt 9999 ]; then
        log_success "Best PyPI mirror: $best_mirror (${best_time}ms)"
    else
        log_warn "All PyPI mirrors slow, using default"
        BEST_PYPI_MIRROR="https://pypi.org/simple"
    fi
}

detect_best_apt_mirror() {
    log_info "Testing APT mirrors..."
    local best_mirror="mirror.yandex.ru"
    local best_time=9999
    local test_urls=(
        "http://deb.debian.org/debian/dists/stable/Release"
        "http://mirror.yandex.ru/debian/dists/stable/Release"
        "http://mirrors.aliyun.com/debian/dists/stable/Release"
    )
    local mirrors=(
        "deb.debian.org"
        "mirror.yandex.ru"
        "mirrors.aliyun.com"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || true
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_APT_MIRROR="$best_mirror"
    if [ "$best_time" -lt 9999 ]; then
        log_success "Best APT mirror: $best_mirror (${best_time}ms)"
    else
        log_warn "All APT mirrors slow, using default"
        BEST_APT_MIRROR="mirror.yandex.ru"
    fi
}

detect_best_mirrors() {
    log_info "Detecting fastest mirrors..."
    detect_best_pypi_mirror
    detect_best_apt_mirror
}

# ==================== Network Fix Functions ====================

# Check Docker Hub (quiet, no logs)
check_docker_hub_quiet() {
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        return 0
    fi
    
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
        return 0
    fi
    
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
    
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ] && grep -q "disable_ipv6 = 1" /etc/sysctl.d/99-vless-tuning.conf; then
        log_success "IPv6 already disabled"
        sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 || true
        return 0
    fi
    
    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    
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
    
    if [ -L /etc/resolv.conf ] && readlink /etc/resolv.conf | grep -q systemd; then
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/dns.conf << 'EOF'
[Resolve]
DNS=1.1.1.1 8.8.8.8 1.0.0.1 8.8.4.4
FallbackDNS=9.9.9.9 149.112.112.112
EOF
        systemctl restart systemd-resolved >/dev/null 2>&1 || true
    else
        chattr -i /etc/resolv.conf >/dev/null 2>&1 || true
        cat > /etc/resolv.conf << 'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 1.0.0.1
nameserver 8.8.4.4
EOF
    fi
    
    log_success "DNS configured"
}

configure_docker_mirrors() {
    log_info "Configuring Docker registry mirrors..."
    
    local docker_config_dir="/etc/docker"
    local daemon_json="$docker_config_dir/daemon.json"
    
    mkdir -p "$docker_config_dir"
    
    if [ -f "$daemon_json" ]; then
        cp "$daemon_json" "${daemon_json}.backup.$(date +%Y%m%d_%H%M%S)"
        
        if command -v jq &>/dev/null; then
            local mirrors_json='["https://mirror.gcr.io","https://registry.docker-cn.com","https://docker.mirrors.ustc.edu.cn"]'
            jq --argjson mirrors "$mirrors_json" '. + {"registry-mirrors": $mirrors}' "$daemon_json" > "${daemon_json}.tmp" && \
                mv "${daemon_json}.tmp" "$daemon_json"
        else
            cat > "$daemon_json" << 'EOF'
{
    "registry-mirrors": [
        "https://mirror.gcr.io",
        "https://registry.docker-cn.com",
        "https://docker.mirrors.ustc.edu.cn"
    ],
    "dns": ["1.1.1.1", "8.8.8.8"]
}
EOF
        fi
    else
        cat > "$daemon_json" << 'EOF'
{
    "registry-mirrors": [
        "https://mirror.gcr.io",
        "https://registry.docker-cn.com",
        "https://docker.mirrors.ustc.edu.cn"
    ],
    "dns": ["1.1.1.1", "8.8.8.8"]
}
EOF
    fi
    
    log_success "Docker mirrors configured"
}

restart_docker() {
    log_info "Restarting Docker service..."
    
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart docker >/dev/null 2>&1 || service docker restart >/dev/null 2>&1 || true
    
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if docker info >/dev/null 2>&1; then
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
    configure_docker_mirrors
    restart_docker
    sleep 3
    return 0
}

# ==================== Core Functions ====================

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "Please run as root: sudo ./deploy.sh"
        exit 1
    fi
}

# Check OS
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

# Install Docker
install_docker() {
    if command -v docker &> /dev/null; then
        DOCKER_VERSION=$(docker --version | cut -d ' ' -f3 | cut -d ',' -f1)
        log_success "Docker already installed: $DOCKER_VERSION"
        return 0
    fi

    log_info "Installing Docker..."

    # Remove old versions
    apt-get remove -y docker docker-engine docker.io containerd runc >/dev/null 2>&1 || true

    # Install dependencies
    run_quiet "apt-get update" apt-get update -qq
    run_quiet "installing dependencies" apt-get install -y -qq ca-certificates curl gnupg lsb-release

    # Add Docker GPG key
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$OS/gpg" 2>/dev/null | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Add repository
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS \
        $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    # Install Docker
    run_quiet "apt-get update" apt-get update -qq
    run_quiet "installing docker" apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Start Docker
    systemctl start docker >/dev/null 2>&1
    systemctl enable docker >/dev/null 2>&1

    log_success "Docker installed successfully"
}

# Generate random API key
generate_api_key() {
    openssl rand -hex 32
}

# Setup environment
setup_env() {
    if [ -f .env ]; then
        log_warn ".env already exists, skipping..."
        # Update PANEL_IP if it changed
        if grep -q "^PANEL_IP=" .env; then
            sed -i "s/^PANEL_IP=.*/PANEL_IP=$PANEL_IP/" .env
        else
            echo "" >> .env
            echo "# Panel IP (set by deploy.sh, used for UFW firewall rule)" >> .env
            echo "PANEL_IP=$PANEL_IP" >> .env
        fi
        return 0
    fi

    log_info "Creating .env from .env.example..."
    cp .env.example .env

    # Generate random API key
    API_KEY=$(generate_api_key)
    sed -i "s/API_KEY=.*/API_KEY=$API_KEY/" .env

    # Add Panel IP (not in .env.example to avoid duplication)
    echo "" >> .env
    echo "# Panel IP (set by deploy.sh, used for UFW firewall rule)" >> .env
    echo "PANEL_IP=$PANEL_IP" >> .env

    log_success "Environment configured"
}

# Generate SSL certificate
setup_ssl() {
    if [ -f nginx/ssl/cert.pem ] && [ -f nginx/ssl/key.pem ]; then
        log_success "SSL certificate already exists"
        return 0
    fi

    log_info "Generating self-signed SSL certificate..."
    
    mkdir -p nginx/ssl
    
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout nginx/ssl/key.pem \
        -out nginx/ssl/cert.pem \
        -subj "/C=US/ST=State/L=City/O=Monitoring/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null

    chmod 600 nginx/ssl/key.pem
    chmod 644 nginx/ssl/cert.pem

    log_success "SSL certificate generated"
}

# Setup certificate auto-renewal cron
setup_cert_renewal_cron() {
    log_info "Setting up certificate auto-renewal..."
    
    # Create renewal script for native HAProxy
    cat > /opt/monitoring-node/renew-certs.sh << 'EOF'
#!/bin/bash
# Auto-renewal script for Let's Encrypt certificates
# Works with native HAProxy (systemd service)

# Check if certbot is available (runs inside monitoring-api container)
if ! docker ps -q -f name=monitoring-api | grep -q .; then
    echo "monitoring-api container not running, skipping renewal"
    exit 0
fi

# Stop HAProxy temporarily for renewal (standalone mode needs port 80)
HAPROXY_WAS_RUNNING=false
if systemctl is-active --quiet haproxy; then
    HAPROXY_WAS_RUNNING=true
    systemctl stop haproxy
fi

# Run certbot renew inside container
docker exec monitoring-api certbot renew --non-interactive --quiet

# Update combined certificates for HAProxy
for cert_dir in /etc/letsencrypt/live/*/; do
    if [ -d "$cert_dir" ]; then
        domain=$(basename "$cert_dir")
        if [ -f "$cert_dir/fullchain.pem" ] && [ -f "$cert_dir/privkey.pem" ]; then
            cat "$cert_dir/fullchain.pem" "$cert_dir/privkey.pem" > "$cert_dir/combined.pem"
            chmod 600 "$cert_dir/combined.pem"
        fi
    fi
done

# Restart HAProxy if it was running
if [ "$HAPROXY_WAS_RUNNING" = true ]; then
    systemctl start haproxy
fi

# Reload HAProxy to pick up new certificates (if running)
if systemctl is-active --quiet haproxy; then
    systemctl reload haproxy 2>/dev/null || true
fi
EOF
    chmod +x /opt/monitoring-node/renew-certs.sh
    
    # Create cron job file
    cat > /etc/cron.d/certbot-renew << EOF
# Auto-renewal of Let's Encrypt certificates
# Runs daily at 3:00 AM
0 3 * * * root /opt/monitoring-node/renew-certs.sh >> /var/log/certbot-renew.log 2>&1
EOF
    chmod 644 /etc/cron.d/certbot-renew
    
    log_success "Certificate auto-renewal cron configured (daily at 3:00 AM)"
}

# Check HAProxy status (native systemd service)
check_haproxy_status() {
    log_info "Checking HAProxy status..."
    
    if systemctl is-active --quiet haproxy 2>/dev/null; then
        log_success "HAProxy service is running (will not be restarted)"
    elif command -v haproxy &>/dev/null; then
        log_info "HAProxy is installed but not running"
    else
        log_info "HAProxy is not installed"
    fi
}

# Ensure /etc/haproxy directory exists on host
ensure_haproxy_dir() {
    if [ ! -d "/etc/haproxy" ]; then
        log_info "Creating /etc/haproxy directory..."
        mkdir -p /etc/haproxy
        chmod 755 /etc/haproxy
    fi
}


# Validate IP address format
validate_ip() {
    local ip=$1
    if [[ $ip =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        IFS='.' read -ra ADDR <<< "$ip"
        for i in "${ADDR[@]}"; do
            if [ "$i" -gt 255 ]; then
                return 1
            fi
        done
        return 0
    fi
    return 1
}

# Ask for panel IP address
ask_panel_ip() {
    echo ""
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}       Panel IP Configuration${NC}"
    echo -e "${YELLOW}========================================${NC}"
    echo ""
    echo -e "Enter the IP address of your monitoring panel."
    echo -e "Port 9100 will be accessible ONLY from this IP."
    echo ""
    
    while true; do
        read -p "Panel IP address: " PANEL_IP
        
        if [ -z "$PANEL_IP" ]; then
            log_error "IP address cannot be empty"
            continue
        fi
        
        if validate_ip "$PANEL_IP"; then
            log_success "IP address validated: $PANEL_IP"
            break
        else
            log_error "Invalid IP address format. Please enter a valid IPv4 address."
        fi
    done
}

# Configure firewall (UFW)
setup_firewall() {
    log_info "Configuring firewall..."
    
    # Check if UFW is installed
    if ! command -v ufw &> /dev/null; then
        log_info "Installing UFW..."
        run_quiet "apt-get update" apt-get update -qq
        run_quiet "installing ufw" apt-get install -y -qq ufw
    fi
    
    local ufw_was_active=false
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw_was_active=true
    fi
    
    # Ask for panel IP
    ask_panel_ip
    
    # Remove old rule if exists (allow from anywhere)
    ufw delete allow 9100/tcp >/dev/null 2>&1 || true
    
    # Allow API port (9100) ONLY from panel IP
    log_info "Adding UFW rule: allow port 9100 from $PANEL_IP"
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp comment "Monitoring API from Panel" >/dev/null 2>&1 || \
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp >/dev/null 2>&1 || true
    
    # Open port 80 for Let's Encrypt certificate verification
    ufw allow 80/tcp comment "HTTP for Let's Encrypt" >/dev/null 2>&1 || true
    
    # Allow SSH to avoid lockout
    ufw allow ssh >/dev/null 2>&1 || ufw allow 22/tcp >/dev/null 2>&1 || true
    
    if [ "$ufw_was_active" = true ]; then
        log_success "Firewall configured"
    else
        log_warn "UFW is not active - rules added but firewall remains disabled"
    fi
    
    log_info "Port 9100 accessible only from: $PANEL_IP"
}

# Build and start containers
start_containers() {
    log_info "Building and starting containers..."
    
    docker compose down >/dev/null 2>&1 || true
    
    # Enable BuildKit for faster builds with cache
    export DOCKER_BUILDKIT=1
    
    # Detect best mirrors first
    detect_best_mirrors
    
    local max_retries=3
    local retry=0
    local build_success=false
    local build_timeout="$DOCKER_BUILD_TIMEOUT"
    
    while [ $retry -lt $max_retries ]; do
        if ! check_docker_hub_quiet; then
            log_warn "Docker Hub is not accessible"
            if [ $retry -eq 0 ]; then
                fix_docker_network
            fi
        fi
        
        # Generate cache bust hash from .env (forces rebuild when any config changes)
        if [ -f .env ]; then
            export CACHE_BUST=$(md5sum .env | cut -d' ' -f1)
            log_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
        fi
        
        # Build arguments with detected mirrors
        local build_args=""
        build_args="--build-arg APT_MIRROR=${BEST_APT_MIRROR:-mirror.yandex.ru}"
        build_args="$build_args --build-arg PIP_INDEX_URL=${BEST_PYPI_MIRROR:-https://pypi.org/simple}"
        build_args="$build_args --build-arg PIP_TIMEOUT=${PIP_TIMEOUT}"
        build_args="$build_args --build-arg APT_TIMEOUT=${APT_TIMEOUT}"
        build_args="$build_args --build-arg CACHE_BUST=${CACHE_BUST:-}"
        
        log_info "Building containers (attempt $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        log_info "BuildKit enabled for faster cached builds"
        log_info "Using mirrors: APT=${BEST_APT_MIRROR:-default}, PyPI=${BEST_PYPI_MIRROR:-default}"
        echo ""
        
        local build_exit_code
        local build_log="/tmp/docker_build_$$.log"
        
        # Run build with timeout, show output in real-time AND capture to log
        set +e
        timeout "$build_timeout" docker build --network=host $build_args -t monitoring-node-api . 2>&1 | tee "$build_log"
        build_exit_code=${PIPESTATUS[0]}
        set -e
        
        echo ""
        
        if [ $build_exit_code -eq 0 ]; then
            build_success=true
            log_success "Docker build completed"
            rm -f "$build_log"
            break
        elif [ $build_exit_code -eq 124 ]; then
            log_error "Build timeout after ${build_timeout}s"
            echo -e "${YELLOW}Build was taking too long. Possible causes:${NC}"
            echo "  - Very slow internet connection"
            echo "  - Package mirrors are unreachable"
            echo "  - Server ran out of memory (check: free -h)"
        else
            log_error "Build failed (exit code: $build_exit_code)"
            echo ""
            echo -e "${YELLOW}Last 30 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -30 "$build_log" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        fi
        
        rm -f "$build_log"
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            log_warn "Build failed, retrying after network fix..."
            fix_docker_network
            # Re-detect mirrors after network fix
            detect_best_mirrors
            sleep 5
        fi
    done
    
    if [ "$build_success" = false ]; then
        log_error "Docker build failed after $max_retries attempts"
        echo ""
        echo "Possible solutions:"
        echo "  1. Check if server has internet access"
        echo "  2. Try using a VPN"
        echo "  3. Check firewall settings"
        echo "  4. Try again later (Docker Hub may be temporarily unavailable)"
        echo "  5. Increase timeout: export DOCKER_BUILD_TIMEOUT=3600"
        echo "  6. Check server memory: free -h (need at least 1GB free)"
        echo ""
        exit 1
    fi
    
    # Ensure /etc/haproxy directory exists for bind mount
    ensure_haproxy_dir
    
    log_info "Starting containers..."
    set +e
    local up_output
    up_output=$(docker compose up -d 2>&1)
    local up_exit_code=$?
    set -e
    
    if [ $up_exit_code -ne 0 ]; then
        log_error "Failed to start containers (exit code: $up_exit_code)"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo "$up_output"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        exit 1
    fi

    log_success "Containers started"
}

# Wait for services
wait_for_services() {
    log_info "Waiting for services to start..."
    
    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if curl -sk https://localhost:9100/health > /dev/null 2>&1; then
            log_success "Services are ready"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    log_error "Services failed to start in time"
    return 1
}

# Check endpoints
check_endpoints() {
    log_info "Checking API endpoints..."
    
    # Get API key from .env
    API_KEY=$(grep "^API_KEY=" .env | cut -d '=' -f2)
    BASE_URL="https://localhost:9100"

    echo ""
    
    # Health (no auth)
    echo -n "  /health: "
    RESPONSE=$(curl -sk "$BASE_URL/health" 2>/dev/null)
    if echo "$RESPONSE" | grep -q '"status":"ok"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    # Metrics (with auth)
    echo -n "  /api/metrics: "
    RESPONSE=$(curl -sk -H "X-API-Key: $API_KEY" "$BASE_URL/api/metrics" 2>/dev/null)
    if echo "$RESPONSE" | grep -q '"cpu"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    # HAProxy API (native systemd service)
    echo -n "  /api/haproxy/status: "
    RESPONSE=$(curl -sk -H "X-API-Key: $API_KEY" "$BASE_URL/api/haproxy/status" 2>/dev/null)
    if echo "$RESPONSE" | grep -q '"running":true'; then
        echo -e "${GREEN}OK (running)${NC}"
    elif echo "$RESPONSE" | grep -q '"running":false'; then
        echo -e "${YELLOW}OK (stopped)${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    # Traffic
    echo -n "  /api/traffic/current: "
    RESPONSE=$(curl -sk -H "X-API-Key: $API_KEY" "$BASE_URL/api/traffic/current" 2>/dev/null)
    if echo "$RESPONSE" | grep -q '"interfaces"'; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAIL${NC}"
    fi

    echo ""
}

# Show status
show_status() {
    # Get API key for final output
    FINAL_API_KEY=$(grep "^API_KEY=" .env | cut -d '=' -f2)
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    
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
    echo "  - Renewal script: $([ -f /opt/monitoring-node/renew-certs.sh ] && echo 'Installed' || echo 'Not found')"
    echo ""
    
    # HAProxy status (native systemd service)
    echo -e "${GREEN}HAProxy (native systemd service):${NC}"
    if systemctl is-active --quiet haproxy 2>/dev/null; then
        echo -e "  - Status: ${GREEN}Running${NC}"
    elif command -v haproxy &>/dev/null; then
        echo -e "  - Status: ${YELLOW}Stopped (start with: systemctl start haproxy)${NC}"
    else
        echo -e "  - Status: ${YELLOW}Not installed${NC}"
    fi
    echo "  - Config: /etc/haproxy/haproxy.cfg"
    echo "  - Manage via panel terminal or: systemctl start/stop/restart haproxy"
    echo ""
    echo "Container status (API only):"
    docker compose ps
    echo ""
    echo "Commands:"
    echo "  docker compose logs -f          # View API logs"
    echo "  docker compose restart          # Restart API"
    echo "  docker compose down             # Stop API"
    echo ""
    echo "  systemctl status haproxy        # HAProxy status"
    echo "  systemctl start haproxy         # Start HAProxy"
    echo "  systemctl stop haproxy          # Stop HAProxy"
    echo "  systemctl restart haproxy       # Restart HAProxy"
    echo ""
    echo -e "${YELLOW}To change Panel IP later:${NC}"
    echo "  ufw delete allow from $PANEL_IP to any port 9100 proto tcp"
    echo "  ufw allow from NEW_IP to any port 9100 proto tcp"
    echo ""
    
    # Final credentials block with pause
    echo ""
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                       CONNECTION DETAILS FOR PANEL                     ║${NC}"
    echo -e "${GREEN}║                      ДАННЫЕ ДЛЯ ПОДКЛЮЧЕНИЯ К ПАНЕЛИ                   ║${NC}"
    echo -e "${GREEN}╠════════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}Server IP / IP сервера:${NC}  ${CYAN}${SERVER_IP}${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}Port / Порт:${NC}             ${CYAN}9100${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}API Key / API ключ:${NC}"
    echo -e "${GREEN}║${NC}  ${BLUE}${FINAL_API_KEY}${NC}"
    echo -e "${GREEN}║${NC}                                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${YELLOW}Copy the data above and add this server to the monitoring panel.${NC}"
    echo -e "${YELLOW}Скопируйте данные выше и добавьте сервер в панель мониторинга.${NC}"
    echo ""
    read -p "Press Enter to finish / Нажмите Enter для завершения..."
}

# Main
main() {
    echo ""
    echo "=========================================="
    echo " Monitoring Node Agent - Deploy Script"
    echo "=========================================="
    echo ""

    check_root
    check_os
    check_haproxy_status        # Check HAProxy status (don't modify it)
    install_docker
    setup_firewall
    setup_env
    setup_ssl
    setup_cert_renewal_cron
    start_containers
    wait_for_services
    check_endpoints
    show_status
}

# Run
cd "$(dirname "$0")"
main "$@"
