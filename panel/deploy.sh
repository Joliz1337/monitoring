#!/bin/bash

set -e

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

check_docker_hub() {
    print_info "Checking Docker Hub availability..."
    
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        print_status "Docker Hub is accessible"
        return 0
    fi
    
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
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
        print_status "IPv6 already disabled in optimization config"
        # Just apply the existing settings
        sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
        sysctl -w net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
        sysctl -w net.ipv6.conf.lo.disable_ipv6=1 2>/dev/null || true
        return 0
    fi
    
    # sysctl settings (separate file if optimizations not applied)
    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true
    sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.lo.disable_ipv6=1 2>/dev/null || true
    
    print_status "IPv6 disabled"
}

configure_dns() {
    print_info "Configuring DNS (1.1.1.1, 8.8.8.8)..."
    
    if [ -f /etc/resolv.conf ] && [ ! -f /etc/resolv.conf.backup ]; then
        cp /etc/resolv.conf /etc/resolv.conf.backup
    fi
    
    if [ -L /etc/resolv.conf ] && readlink /etc/resolv.conf | grep -q systemd; then
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/dns.conf << 'EOF'
[Resolve]
DNS=1.1.1.1 8.8.8.8 1.0.0.1 8.8.4.4
FallbackDNS=9.9.9.9 149.112.112.112
EOF
        systemctl restart systemd-resolved 2>/dev/null || true
    else
        chattr -i /etc/resolv.conf 2>/dev/null || true
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
    
    systemctl daemon-reload 2>/dev/null || true
    systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
    
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
    print_info "Attempting to fix network issues..."
    
    echo ""
    print_info "Applying fix 1/3: IPv6"
    disable_ipv6
    
    echo ""
    print_info "Applying fix 2/3: DNS"
    configure_dns
    
    echo ""
    print_info "Applying fix 3/3: Docker mirrors"
    configure_docker_mirrors
    
    echo ""
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
        apt-get update
        apt-get install -y ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    elif [ -f /etc/redhat-release ]; then
        yum install -y yum-utils
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
        systemctl start docker
        systemctl enable docker
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
    print_info "Configuring firewall (opening ports 80, 443)..."
    
    local firewall_configured=false
    
    # Try UFW first
    if command -v ufw &> /dev/null; then
        print_info "Using UFW..."
        
        # Check if UFW was already active before we make changes
        local ufw_was_active=false
        if ufw status | grep -q "Status: active"; then
            ufw_was_active=true
        fi
        
        # Add rules (they will be stored even if UFW is inactive)
        ufw allow 22/tcp 2>/dev/null || true
        ufw allow 80/tcp 2>/dev/null || true
        ufw allow 443/tcp 2>/dev/null || true
        
        # Only enable UFW if it was already active
        # If UFW was disabled, keep it disabled - rules are added but won't be applied
        if [ "$ufw_was_active" = true ]; then
            print_status "UFW: ports 22, 80, 443 opened (firewall active)"
        else
            print_warning "UFW is not active - rules added but firewall remains disabled"
            print_info "To enable firewall manually: ufw --force enable"
        fi
        
        firewall_configured=true
    fi
    
    # Also configure iptables directly as fallback
    if command -v iptables &> /dev/null; then
        print_info "Configuring iptables..."
        
        # Accept incoming on ports 80 and 443
        iptables -I INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
        iptables -I INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
        
        # Save iptables rules
        if command -v netfilter-persistent &> /dev/null; then
            netfilter-persistent save 2>/dev/null || true
        elif [ -f /etc/debian_version ]; then
            iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
        elif [ -f /etc/redhat-release ]; then
            service iptables save 2>/dev/null || true
        fi
        
        firewall_configured=true
        print_status "iptables: ports 80, 443 opened"
    fi
    
    # Try firewalld (CentOS/RHEL)
    if command -v firewall-cmd &> /dev/null; then
        print_info "Configuring firewalld..."
        firewall-cmd --permanent --add-port=80/tcp 2>/dev/null || true
        firewall-cmd --permanent --add-port=443/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
        firewall_configured=true
        print_status "firewalld: ports 80, 443 opened"
    fi
    
    if [ "$firewall_configured" = false ]; then
        print_warning "No firewall tool found. Make sure ports 80 and 443 are open!"
    fi
    
    # Verify port 80 is accessible
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
        apt-get update
        apt-get install -y certbot
    elif [ -f /etc/redhat-release ]; then
        if command -v dnf &> /dev/null; then
            dnf install -y certbot
        else
            yum install -y certbot
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
    # Stop our containers if running
    docker compose down 2>/dev/null || true
    
    # Stop common services that use port 80
    systemctl stop nginx 2>/dev/null || true
    systemctl stop apache2 2>/dev/null || true
    systemctl stop httpd 2>/dev/null || true
    
    # Wait a moment for ports to be released
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
    
    if [ -z "$DOMAIN" ]; then
        print_error "DOMAIN variable is empty!"
        exit 1
    fi
    
    export DOMAIN
    envsubst '${DOMAIN}' < nginx/nginx.conf.template > nginx/nginx.conf
    
    print_status "nginx.conf generated for ${DOMAIN}"
}

build_and_start() {
    print_info "Building and starting containers..."
    
    docker compose down --remove-orphans 2>/dev/null || true
    
    # Try to build with retry on network errors
    local max_retries=3
    local retry=0
    local build_success=false
    
    while [ $retry -lt $max_retries ]; do
        # Check Docker Hub availability first
        if ! check_docker_hub; then
            if [ $retry -eq 0 ]; then
                echo ""
                fix_docker_network
                echo ""
            fi
        fi
        
        print_info "Building containers (attempt $((retry + 1))/$max_retries)..."
        
        if docker compose build --no-cache 2>&1; then
            build_success=true
            break
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            print_warning "Build failed, retrying after network fix..."
            echo ""
            fix_docker_network
            echo ""
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
        echo ""
        exit 1
    fi
    
    docker compose up -d
    
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
