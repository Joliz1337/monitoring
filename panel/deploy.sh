#!/bin/bash

set -e

# Build log file for error reporting
BUILD_LOG="/tmp/docker_build_$$.log"

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[✗] Script interrupted or failed (exit code: $exit_code)\033[0m"
        # Show last lines from build log if exists
        if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
            echo -e "\033[0;31m[✗] Last 50 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -50 "$BUILD_LOG"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
        fi
        rm -f "$BUILD_LOG"
    fi
    exit $exit_code
}
trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[✗] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[✗] Terminated by signal\033[0m"; exit 143' TERM

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[✓]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[!]${NC} $1"; }
print_error() { echo -e "${RED}[✗]${NC} $1"; }
print_info() { echo -e "${CYAN}[i]${NC} $1"; }

# Timeouts (in seconds)
DOCKER_BUILD_TIMEOUT="${DOCKER_BUILD_TIMEOUT:-1800}"  # 30 min default
APT_TIMEOUT="${APT_TIMEOUT:-120}"
PIP_TIMEOUT="${PIP_TIMEOUT:-120}"
NPM_TIMEOUT="${NPM_TIMEOUT:-120000}"  # npm uses milliseconds

# Best mirrors (will be detected)
BEST_PYPI_MIRROR=""
BEST_NPM_MIRROR=""
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
        print_error "$desc - failed (exit code: $exit_code)"
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
    print_info "Testing PyPI mirrors..."
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
        print_status "Best PyPI mirror: $best_mirror (${best_time}ms)"
    else
        print_warning "All PyPI mirrors slow, using default"
        BEST_PYPI_MIRROR="https://pypi.org/simple"
    fi
}

detect_best_npm_mirror() {
    print_info "Testing npm mirrors..."
    local best_mirror="https://registry.npmjs.org"
    local best_time=9999
    local test_urls=(
        "https://registry.npmjs.org/npm"
        "https://registry.npmmirror.com/npm"
    )
    local mirrors=(
        "https://registry.npmjs.org"
        "https://registry.npmmirror.com"
    )
    
    for i in "${!test_urls[@]}"; do
        local time_ms
        time_ms=$(test_mirror_speed "${test_urls[$i]}" 5) || true
        if [ "$time_ms" -lt "$best_time" ]; then
            best_time=$time_ms
            best_mirror="${mirrors[$i]}"
        fi
    done
    
    BEST_NPM_MIRROR="$best_mirror"
    if [ "$best_time" -lt 9999 ]; then
        print_status "Best npm mirror: $best_mirror (${best_time}ms)"
    else
        print_warning "All npm mirrors slow, using default"
        BEST_NPM_MIRROR="https://registry.npmjs.org"
    fi
}

detect_best_apt_mirror() {
    print_info "Testing APT mirrors..."
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
        print_status "Best APT mirror: $best_mirror (${best_time}ms)"
    else
        print_warning "All APT mirrors slow, using default"
        BEST_APT_MIRROR="mirror.yandex.ru"
    fi
}

detect_best_mirrors() {
    print_info "Detecting fastest mirrors for your location..."
    detect_best_pypi_mirror
    detect_best_npm_mirror
    detect_best_apt_mirror
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Minimum days before certificate expiration to trigger renewal
CERT_RENEWAL_DAYS=30

# Docker mirror list
DOCKER_MIRRORS=(
    "https://mirror.gcr.io"
    "https://registry.docker-cn.com"
    "https://docker.mirrors.ustc.edu.cn"
)

echo ""
echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       Monitoring Panel Deployment          ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
echo ""

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
    print_info "Checking Docker Hub availability..."
    
    if check_docker_hub_quiet; then
        print_status "Docker Hub is accessible"
        return 0
    fi
    
    print_warning "Docker Hub is not accessible"
    return 1
}

disable_ipv6() {
    print_info "Disabling IPv6..."
    
    # Check if IPv6 is already disabled in optimization config
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ] && grep -q "disable_ipv6 = 1" /etc/sysctl.d/99-vless-tuning.conf; then
        print_status "IPv6 already disabled"
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
    
    print_status "IPv6 disabled"
}

configure_dns() {
    print_info "Configuring DNS..."
    
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
    
    print_status "DNS configured"
}

configure_docker_mirrors() {
    print_info "Configuring Docker registry mirrors..."
    
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
    
    print_status "Docker mirrors configured"
}

restart_docker() {
    print_info "Restarting Docker service..."
    
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart docker >/dev/null 2>&1 || service docker restart >/dev/null 2>&1 || true
    
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if docker info >/dev/null 2>&1; then
            print_status "Docker service restarted"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done
    
    print_warning "Docker may need manual restart"
    return 1
}

fix_docker_network() {
    print_info "Fixing network issues..."
    disable_ipv6
    configure_dns
    configure_docker_mirrors
    restart_docker
    sleep 3
    return 0
}

# ==================== Core Functions ====================

check_docker() {
    if command -v docker &> /dev/null; then
        print_status "Docker is installed"
        return 0
    fi
    return 1
}

install_docker() {
    print_info "Installing Docker..."
    
    if [ -f /etc/debian_version ]; then
        run_quiet "apt-get update" apt-get update -qq
        run_quiet "installing dependencies" apt-get install -y -qq ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg 2>/dev/null | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
        run_quiet "apt-get update" apt-get update -qq
        run_quiet "installing docker" apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    elif [ -f /etc/redhat-release ]; then
        run_quiet "installing yum-utils" yum install -y -q yum-utils
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
        run_quiet "installing docker" yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
        systemctl start docker >/dev/null 2>&1
        systemctl enable docker >/dev/null 2>&1
    else
        print_error "Unsupported OS. Please install Docker manually."
        exit 1
    fi
    
    print_status "Docker installed successfully"
}

generate_random() {
    local length=$1
    openssl rand -hex $((length / 2)) 2>/dev/null || cat /dev/urandom | tr -dc 'a-zA-Z0-9' | head -c $length
}

prompt_domain() {
    if [ -n "$DOMAIN" ]; then
        return
    fi
    
    echo ""
    echo -e "${YELLOW}========================================${NC}"
    echo -e "${YELLOW}         Domain Configuration          ${NC}"
    echo -e "${YELLOW}========================================${NC}"
    echo ""
    echo -e "Enter the domain for your monitoring panel."
    echo -e "Make sure DNS is already pointing to this server!"
    echo ""
    
    read -p "Domain (e.g., panel.example.com): " DOMAIN
    
    if [ -z "$DOMAIN" ]; then
        print_error "Domain is required"
        exit 1
    fi
    
    # Validate domain format
    if ! echo "$DOMAIN" | grep -qE '^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$'; then
        print_error "Invalid domain format: ${DOMAIN}"
        exit 1
    fi
    
    print_status "Domain set: ${DOMAIN}"
}

setup_firewall() {
    print_info "Configuring firewall..."
    
    local firewall_configured=false
    
    # Try UFW first
    if command -v ufw &> /dev/null; then
        local ufw_was_active=false
        if ufw status 2>/dev/null | grep -q "Status: active"; then
            ufw_was_active=true
        fi
        
        ufw allow 22/tcp >/dev/null 2>&1 || true
        ufw allow 80/tcp >/dev/null 2>&1 || true
        ufw allow 443/tcp >/dev/null 2>&1 || true
        
        if [ "$ufw_was_active" = true ]; then
            print_status "UFW: ports 22, 80, 443 opened"
        else
            print_warning "UFW is not active - rules added but firewall remains disabled"
        fi
        
        firewall_configured=true
    fi
    
    # Also configure iptables directly as fallback
    if command -v iptables &> /dev/null; then
        iptables -I INPUT -p tcp --dport 80 -j ACCEPT >/dev/null 2>&1 || true
        iptables -I INPUT -p tcp --dport 443 -j ACCEPT >/dev/null 2>&1 || true
        
        if command -v netfilter-persistent &> /dev/null; then
            netfilter-persistent save >/dev/null 2>&1 || true
        elif [ -f /etc/debian_version ]; then
            iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
        elif [ -f /etc/redhat-release ]; then
            service iptables save >/dev/null 2>&1 || true
        fi
        
        firewall_configured=true
        print_status "iptables: ports 80, 443 opened"
    fi
    
    # Try firewalld (CentOS/RHEL)
    if command -v firewall-cmd &> /dev/null; then
        firewall-cmd --permanent --add-port=80/tcp >/dev/null 2>&1 || true
        firewall-cmd --permanent --add-port=443/tcp >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1 || true
        firewall_configured=true
        print_status "firewalld: ports 80, 443 opened"
    fi
    
    if [ "$firewall_configured" = false ]; then
        print_warning "No firewall tool found. Make sure ports 80 and 443 are open!"
    fi
    
    sleep 1
    if ss -tuln 2>/dev/null | grep -q ':80 '; then
        print_warning "Port 80 is currently in use by another service"
    fi
}

install_certbot() {
    if command -v certbot &> /dev/null; then
        print_status "Certbot is already installed"
        return 0
    fi
    
    print_info "Installing Certbot..."
    
    if [ -f /etc/debian_version ]; then
        run_quiet "apt-get update" apt-get update -qq
        run_quiet "installing certbot" apt-get install -y -qq certbot
    elif [ -f /etc/redhat-release ]; then
        if command -v dnf &> /dev/null; then
            run_quiet "installing certbot" dnf install -y -q certbot
        else
            run_quiet "installing certbot" yum install -y -q certbot
        fi
    else
        print_error "Unsupported OS for automatic Certbot installation"
        exit 1
    fi
    
    print_status "Certbot installed successfully"
}

# Get certificate expiration days
get_cert_days_remaining() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
    
    if [ ! -f "$cert_path" ]; then
        echo "-1"
        return
    fi
    
    local expiry_date
    expiry_date=$(openssl x509 -enddate -noout -in "$cert_path" 2>/dev/null | cut -d= -f2)
    
    if [ -z "$expiry_date" ]; then
        echo "-1"
        return
    fi
    
    local expiry_epoch
    local now_epoch
    expiry_epoch=$(date -d "$expiry_date" +%s 2>/dev/null)
    now_epoch=$(date +%s)
    
    if [ -z "$expiry_epoch" ]; then
        echo "-1"
        return
    fi
    
    local days_remaining=$(( (expiry_epoch - now_epoch) / 86400 ))
    echo "$days_remaining"
}

# Stop services that might use port 80
stop_port_80_services() {
    docker compose down >/dev/null 2>&1 || true
    systemctl stop nginx >/dev/null 2>&1 || true
    systemctl stop apache2 >/dev/null 2>&1 || true
    systemctl stop httpd >/dev/null 2>&1 || true
    sleep 2
}

# Obtain SSL certificate
obtain_certificate() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"
    
    print_info "Obtaining Let's Encrypt certificate for ${DOMAIN}..."
    
    # Stop services using port 80
    stop_port_80_services
    
    # Check if port 80 is available
    if netstat -tuln 2>/dev/null | grep -q ':80 ' || ss -tuln 2>/dev/null | grep -q ':80 '; then
        print_error "Port 80 is still in use. Please stop the service using it."
        print_info "Run: netstat -tuln | grep :80  or  ss -tuln | grep :80"
        exit 1
    fi
    
    # Request certificate
    if certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email \
        -d "$DOMAIN" 2>&1; then
        print_status "Certificate obtained successfully!"
        return 0
    else
        print_error "Failed to obtain certificate"
        print_info "Make sure:"
        echo "  1. Domain ${DOMAIN} points to this server's IP"
        echo "  2. Port 80 is open and accessible from the internet"
        echo "  3. No other service is using port 80"
        exit 1
    fi
}

# Renew certificate
renew_certificate() {
    print_info "Renewing certificate for ${DOMAIN}..."
    
    stop_port_80_services
    
    if certbot renew --cert-name "$DOMAIN" --standalone --non-interactive 2>&1; then
        print_status "Certificate renewed successfully!"
        return 0
    else
        print_error "Failed to renew certificate"
        return 1
    fi
}

# Main SSL certificate management
setup_ssl_certificate() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"
    
    # Install certbot if needed
    install_certbot
    
    # Check if certificate exists
    if [ -f "${cert_path}/fullchain.pem" ] && [ -f "${cert_path}/privkey.pem" ]; then
        local days_remaining
        days_remaining=$(get_cert_days_remaining)
        
        if [ "$days_remaining" -lt 0 ]; then
            print_warning "Certificate exists but cannot read expiration date"
            print_info "Attempting to renew..."
            renew_certificate
        elif [ "$days_remaining" -le 0 ]; then
            print_error "Certificate has EXPIRED!"
            print_info "Renewing certificate..."
            renew_certificate
        elif [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
            print_warning "Certificate expires in ${days_remaining} days"
            echo ""
            read -p "Renew certificate now? (Y/n): " renew_choice
            if [ "$renew_choice" != "n" ] && [ "$renew_choice" != "N" ]; then
                renew_certificate
            else
                print_info "Skipping renewal. Certificate valid for ${days_remaining} days."
            fi
        else
            print_status "Certificate valid for ${days_remaining} days"
        fi
    else
        print_info "No certificate found for ${DOMAIN}"
        obtain_certificate
    fi
    
    # Final verification
    if [ ! -f "${cert_path}/fullchain.pem" ] || [ ! -f "${cert_path}/privkey.pem" ]; then
        print_error "SSL certificate not found after setup!"
        exit 1
    fi
    
    # Show certificate info
    local final_days
    final_days=$(get_cert_days_remaining)
    if [ "$final_days" -gt 0 ]; then
        print_status "SSL certificate ready (expires in ${final_days} days)"
    fi
}

# Setup automatic renewal cron job
setup_cert_renewal_cron() {
    local cron_job="0 3 * * * certbot renew --quiet --deploy-hook 'docker compose -f ${SCRIPT_DIR}/docker-compose.yml restart nginx'"
    
    # Check if cron job already exists
    if crontab -l 2>/dev/null | grep -q "certbot renew"; then
        print_status "Certificate auto-renewal cron job already exists"
        return 0
    fi
    
    print_info "Setting up automatic certificate renewal..."
    
    # Add cron job
    (crontab -l 2>/dev/null; echo "$cron_job") | crontab -
    
    print_status "Auto-renewal cron job added (daily at 3 AM)"
}

generate_env() {
    if [ -f .env ]; then
        print_warning ".env file exists. Checking configuration..."
        source .env
        
        # Update domain if changed
        if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "$(grep '^DOMAIN=' .env | cut -d= -f2)" ]; then
            sed -i "s/^DOMAIN=.*/DOMAIN=${DOMAIN}/" .env
            print_info "Domain updated in .env"
        fi
        
        if [ -z "$PANEL_UID" ] || [ "$PANEL_UID" = "changeme" ]; then
            print_info "Regenerating credentials..."
        else
            print_status "Using existing configuration"
            return
        fi
    fi
    
    print_info "Generating .env configuration..."
    
    PANEL_UID=$(generate_random 16)
    PANEL_PASSWORD=$(generate_random 32)
    JWT_SECRET=$(generate_random 64)
    
    cat > .env << EOF
# Domain (required for SSL)
DOMAIN=${DOMAIN}

# Panel Authentication (auto-generated)
PANEL_UID=${PANEL_UID}
PANEL_PASSWORD=${PANEL_PASSWORD}

# JWT Settings
JWT_SECRET=${JWT_SECRET}
JWT_EXPIRE_MINUTES=1440

# Security
MAX_FAILED_ATTEMPTS=5
BAN_DURATION_SECONDS=900

# Ports
PANEL_PORT=443
PANEL_HTTP_PORT=80
EOF
    
    chmod 600 .env
    print_status ".env file generated"
}

generate_nginx_config() {
    print_info "Generating nginx configuration..."
    
    # Use separate script if available
    if [ -f "$SCRIPT_DIR/scripts/generate-nginx-config.sh" ]; then
        chmod +x "$SCRIPT_DIR/scripts/generate-nginx-config.sh"
        bash "$SCRIPT_DIR/scripts/generate-nginx-config.sh" "$SCRIPT_DIR"
        return
    fi
    
    # Fallback inline generation
    if [ -z "$DOMAIN" ]; then
        print_error "DOMAIN variable is empty!"
        exit 1
    fi
    
    if [ -z "$PANEL_UID" ]; then
        print_error "PANEL_UID variable is empty!"
        exit 1
    fi
    
    export DOMAIN PANEL_UID
    envsubst '${DOMAIN} ${PANEL_UID}' < nginx/nginx.conf.template > nginx/nginx.conf
    
    print_status "nginx.conf generated for ${DOMAIN} with UID protection"
}

build_and_start() {
    print_info "Building and starting containers..."
    
    docker compose down --remove-orphans >/dev/null 2>&1 || true
    
    # Enable BuildKit for faster builds with cache
    export DOCKER_BUILDKIT=1
    export COMPOSE_DOCKER_CLI_BUILD=1
    
    # Detect best mirrors first
    detect_best_mirrors
    
    local max_retries=3
    local retry=0
    local build_success=false
    local build_timeout="$DOCKER_BUILD_TIMEOUT"
    
    while [ $retry -lt $max_retries ]; do
        if ! check_docker_hub_quiet; then
            print_warning "Docker Hub is not accessible"
            if [ $retry -eq 0 ]; then
                fix_docker_network
            fi
        fi
        
        # Generate cache bust hash from .env (forces rebuild when any config changes)
        if [ -f .env ]; then
            export CACHE_BUST=$(md5sum .env | cut -d' ' -f1)
            print_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
        fi
        
        # Build arguments with detected mirrors
        local build_args=""
        build_args="--build-arg APT_MIRROR=${BEST_APT_MIRROR:-mirror.yandex.ru}"
        build_args="$build_args --build-arg PIP_INDEX_URL=${BEST_PYPI_MIRROR:-https://pypi.org/simple}"
        build_args="$build_args --build-arg NPM_REGISTRY=${BEST_NPM_MIRROR:-https://registry.npmmirror.com}"
        build_args="$build_args --build-arg PIP_TIMEOUT=${PIP_TIMEOUT}"
        build_args="$build_args --build-arg APT_TIMEOUT=${APT_TIMEOUT}"
        build_args="$build_args --build-arg NPM_TIMEOUT=${NPM_TIMEOUT}"
        build_args="$build_args --build-arg CACHE_BUST=${CACHE_BUST:-}"
        
        print_info "Building containers (attempt $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        print_info "BuildKit enabled for faster cached builds"
        print_info "Using mirrors: APT=${BEST_APT_MIRROR:-default}, PyPI=${BEST_PYPI_MIRROR:-default}, npm=${BEST_NPM_MIRROR:-default}"
        
        local build_exit_code
        
        # Run build in background, capture output to log file
        set +e
        timeout "$build_timeout" docker compose build --parallel $build_args > "$BUILD_LOG" 2>&1 &
        local build_pid=$!
        
        # Show progress while building
        local dots=""
        while kill -0 $build_pid 2>/dev/null; do
            dots="${dots}."
            if [ ${#dots} -gt 3 ]; then dots="."; fi
            # Show current step from log if available
            local current_step=$(grep -oE 'Step [0-9]+/[0-9]+|#[0-9]+ \[[0-9]+/[0-9]+\]' "$BUILD_LOG" 2>/dev/null | tail -1)
            if [ -n "$current_step" ]; then
                printf "\r${CYAN}[i]${NC} Building${dots} %-30s" "($current_step)"
            else
                printf "\r${CYAN}[i]${NC} Building${dots}   "
            fi
            sleep 2
        done
        printf "\r%-60s\r" " "  # Clear progress line
        
        wait $build_pid
        build_exit_code=$?
        set -e
        
        if [ $build_exit_code -eq 0 ]; then
            build_success=true
            print_status "Docker build completed"
            rm -f "$BUILD_LOG"
            break
        elif [ $build_exit_code -eq 124 ]; then
            print_error "Build timeout after ${build_timeout}s"
            echo -e "${YELLOW}Build was taking too long. Possible causes:${NC}"
            echo "  - Very slow internet connection"
            echo "  - Package mirrors are unreachable"
            echo "  - Server ran out of memory (check: free -h)"
            echo ""
            echo -e "${YELLOW}Last 50 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        else
            print_error "Build failed (exit code: $build_exit_code)"
            echo ""
            echo -e "${YELLOW}Last 50 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            print_warning "Build failed, retrying after network fix..."
            fix_docker_network
            # Re-detect mirrors after network fix
            detect_best_mirrors
            sleep 5
        fi
    done
    
    if [ "$build_success" = false ]; then
        print_error "Docker build failed after $max_retries attempts"
        echo ""
        echo "Possible solutions:"
        echo "  1. Check if server has internet access"
        echo "  2. Try using a VPN"
        echo "  3. Check firewall settings"
        echo "  4. Try again later (Docker Hub may be temporarily unavailable)"
        echo "  5. Increase timeout: export DOCKER_BUILD_TIMEOUT=3600"
        echo "  6. Check server memory: free -h (need at least 2GB free)"
        echo ""
        exit 1
    fi
    
    print_info "Starting containers..."
    set +e
    local up_output
    up_output=$(docker compose up -d 2>&1)
    local up_exit_code=$?
    set -e
    
    if [ $up_exit_code -ne 0 ]; then
        print_error "Failed to start containers (exit code: $up_exit_code)"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo "$up_output"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        exit 1
    fi
    
    print_status "Containers started"
}

wait_for_health() {
    print_info "Waiting for services to be ready..."
    
    source .env
    local max_attempts=30
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        if curl -sk "https://${DOMAIN}/health" > /dev/null 2>&1; then
            print_status "Services are healthy"
            return 0
        fi
        if curl -sk "https://localhost/health" > /dev/null 2>&1; then
            print_status "Services are healthy"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
    
    print_warning "Health check timed out, but services may still be starting"
}

print_credentials() {
    source .env
    
    local days_remaining
    days_remaining=$(get_cert_days_remaining)
    
    echo ""
    echo -e "${CYAN}Commands:${NC}"
    echo "  docker compose logs -f     # View logs"
    echo "  docker compose restart     # Restart services"
    echo "  docker compose down        # Stop services"
    echo "  certbot certificates       # View certificate status"
    echo ""
    echo -e "${CYAN}SSL Certificate:${NC}"
    if [ "$days_remaining" -gt 0 ]; then
        if [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
            echo -e "  ${YELLOW}Expires in ${days_remaining} days (renewal recommended)${NC}"
        else
            echo -e "  ${GREEN}Valid for ${days_remaining} days${NC}"
        fi
    else
        echo -e "  ${RED}Check certificate status${NC}"
    fi
    echo -e "  Auto-renewal: Enabled (cron daily at 3 AM)"
    echo ""
    
    # Final credentials block with pause
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║                       ДАННЫЕ ДЛЯ ВХОДА В ПАНЕЛЬ                      ║${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}URL панели:${NC}                                                        ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${CYAN}https://${DOMAIN}/${PANEL_UID}${NC}"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${YELLOW}Пароль:${NC}                                                             ${GREEN}║${NC}"
    echo -e "${GREEN}║${NC}  ${CYAN}${PANEL_PASSWORD}${NC}"
    echo -e "${GREEN}║${NC}                                                                      ${GREEN}║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${RED}ВАЖНО: Сохраните эти данные! После закрытия они не будут показаны снова.${NC}"
    echo ""
    read -p "Нажмите Enter для завершения..."
}

main() {
    if [ "$EUID" -ne 0 ] && ! groups | grep -q docker; then
        print_error "Please run as root or add user to docker group"
        exit 1
    fi
    
    # Check/install Docker
    if ! check_docker; then
        if [ "$EUID" -eq 0 ]; then
            install_docker
        else
            print_error "Docker not found. Please install Docker or run as root."
            exit 1
        fi
    fi
    
    # Ask for domain first
    prompt_domain
    
    # Setup firewall (need port 80 for Let's Encrypt)
    if [ "$EUID" -eq 0 ]; then
        setup_firewall
    fi
    
    # Setup SSL certificate (auto-install certbot, obtain/renew cert)
    setup_ssl_certificate
    
    # Setup auto-renewal cron
    setup_cert_renewal_cron
    
    # Generate configs
    generate_env
    source .env
    generate_nginx_config
    
    # Build and start
    build_and_start
    wait_for_health
    print_credentials
}

main "$@"
