#!/bin/bash
#
# Monitoring Panel - Auto Deploy Script
#

# ==================== Safety Settings ====================

set +e  # Handle errors manually

LOCKFILE="/tmp/monitoring-panel-deploy.lock"
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
TIMEOUT_CERTBOT=300

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
            echo -e "\033[0;31m[ERROR] Last 30 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -30 "$BUILD_LOG" 2>/dev/null || true
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
CYAN='\033[0;36m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[✓]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[!]${NC} $1"; }
print_error() { echo -e "${RED}[✗]${NC} $1"; }
print_info() { echo -e "${CYAN}[i]${NC} $1"; }

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
            print_warning "$desc - timeout (${timeout_sec}s, attempt $attempt/$max_retries)"
        else
            print_warning "$desc - failed (exit $exit_code, attempt $attempt/$max_retries)"
        fi
        
        if [ $attempt -lt $max_retries ]; then
            sleep "$delay"
        fi
        
        attempt=$((attempt + 1))
    done
    
    print_error "$desc - failed after $max_retries attempts"
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
        print_error "$desc - failed (exit code: $exit_code)"
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

# ==================== Configuration ====================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

CERT_RENEWAL_DAYS=30

echo ""
echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       Monitoring Panel Deployment          ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
echo ""

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
    
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ] && grep -q "disable_ipv6 = 1" /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null; then
        print_status "IPv6 already disabled"
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
    
    print_status "IPv6 disabled"
}

configure_dns() {
    print_info "Configuring DNS..."
    
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
    
    print_status "DNS configured"
}

configure_docker_dns() {
    print_info "Configuring Docker DNS..."
    
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
    
    print_status "Docker DNS configured"
}

restart_docker() {
    print_info "Restarting Docker service..."
    
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl restart docker >/dev/null 2>&1 || \
        timeout "$TIMEOUT_SYSTEMCTL" service docker restart >/dev/null 2>&1 || true
    
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if timeout 5 docker info >/dev/null 2>&1; then
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
    configure_docker_dns
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
        run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
            apt-get update -qq || print_warning "apt update had issues"
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing dependencies" \
            apt-get install -y -qq ca-certificates curl gnupg || {
            print_error "Failed to install dependencies"
            return 1
        }
        install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true
        
        if ! timeout "$TIMEOUT_CURL" curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg 2>/dev/null | \
            gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null; then
            print_error "Failed to download Docker GPG key"
            return 1
        fi
        chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true
        
        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
        
        run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
            apt-get update -qq || print_warning "apt update had issues"
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing docker" \
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin || {
            print_error "Failed to install Docker"
            return 1
        }
    elif [ -f /etc/redhat-release ]; then
        run_quiet "installing yum-utils" yum install -y -q yum-utils || return 1
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
        run_quiet "installing docker" yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin || return 1
        timeout "$TIMEOUT_SYSTEMCTL" systemctl start docker >/dev/null 2>&1 || true
        timeout "$TIMEOUT_SYSTEMCTL" systemctl enable docker >/dev/null 2>&1 || true
    else
        print_error "Unsupported OS. Please install Docker manually."
        return 1
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
    
    local max_attempts=5
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        DOMAIN=$(safe_read "Domain (e.g., panel.example.com): " "" 60)
        
        if [ -z "$DOMAIN" ]; then
            print_error "Domain is required"
            attempt=$((attempt + 1))
            continue
        fi
        
        if echo "$DOMAIN" | grep -qE '^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$'; then
            print_status "Domain set: ${DOMAIN}"
            return 0
        else
            print_error "Invalid domain format: ${DOMAIN}"
            attempt=$((attempt + 1))
        fi
    done
    
    print_error "Too many invalid attempts"
    exit 1
}

setup_firewall() {
    print_info "Configuring firewall..."
    
    local firewall_configured=false
    
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
        run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
            apt-get update -qq || true
        run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "installing certbot" \
            apt-get install -y -qq certbot || {
            print_error "Failed to install certbot"
            return 1
        }
    elif [ -f /etc/redhat-release ]; then
        if command -v dnf &> /dev/null; then
            run_quiet "installing certbot" dnf install -y -q certbot || return 1
        else
            run_quiet "installing certbot" yum install -y -q certbot || return 1
        fi
    else
        print_error "Unsupported OS for automatic Certbot installation"
        return 1
    fi
    
    print_status "Certbot installed successfully"
}

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

stop_port_80_services() {
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl stop nginx >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl stop apache2 >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl stop httpd >/dev/null 2>&1 || true
    sleep 2
}

obtain_certificate() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"
    
    print_info "Obtaining Let's Encrypt certificate for ${DOMAIN}..."
    
    stop_port_80_services
    
    if netstat -tuln 2>/dev/null | grep -q ':80 ' || ss -tuln 2>/dev/null | grep -q ':80 '; then
        print_error "Port 80 is still in use. Please stop the service using it."
        print_info "Run: ss -tuln | grep :80"
        return 1
    fi
    
    if timeout "$TIMEOUT_CERTBOT" certbot certonly --standalone --non-interactive --agree-tos \
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
        return 1
    fi
}

renew_certificate() {
    print_info "Renewing certificate for ${DOMAIN}..."
    
    stop_port_80_services
    
    if timeout "$TIMEOUT_CERTBOT" certbot renew --cert-name "$DOMAIN" --standalone --non-interactive 2>&1; then
        print_status "Certificate renewed successfully!"
        return 0
    else
        print_error "Failed to renew certificate"
        return 1
    fi
}

setup_ssl_certificate() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"
    
    install_certbot || return 1
    
    if [ -f "${cert_path}/fullchain.pem" ] && [ -f "${cert_path}/privkey.pem" ]; then
        local days_remaining
        days_remaining=$(get_cert_days_remaining)
        
        if [ "$days_remaining" -lt 0 ]; then
            print_warning "Certificate exists but cannot read expiration date"
            print_info "Attempting to renew..."
            renew_certificate || return 1
        elif [ "$days_remaining" -le 0 ]; then
            print_error "Certificate has EXPIRED!"
            print_info "Renewing certificate..."
            renew_certificate || return 1
        elif [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
            print_warning "Certificate expires in ${days_remaining} days"
            echo ""
            local renew_choice
            renew_choice=$(safe_read "Renew certificate now? (Y/n): " "Y" 30)
            if [ "$renew_choice" != "n" ] && [ "$renew_choice" != "N" ]; then
                renew_certificate || return 1
            else
                print_info "Skipping renewal. Certificate valid for ${days_remaining} days."
            fi
        else
            print_status "Certificate valid for ${days_remaining} days"
        fi
    else
        print_info "No certificate found for ${DOMAIN}"
        obtain_certificate || return 1
    fi
    
    if [ ! -f "${cert_path}/fullchain.pem" ] || [ ! -f "${cert_path}/privkey.pem" ]; then
        print_error "SSL certificate not found after setup!"
        return 1
    fi
    
    local final_days
    final_days=$(get_cert_days_remaining)
    if [ "$final_days" -gt 0 ]; then
        print_status "SSL certificate ready (expires in ${final_days} days)"
    fi
}

setup_cert_renewal_cron() {
    local cron_job="0 3 * * * certbot renew --quiet --deploy-hook 'docker compose -f ${SCRIPT_DIR}/docker-compose.yml restart nginx'"
    
    if crontab -l 2>/dev/null | grep -q "certbot renew"; then
        print_status "Certificate auto-renewal cron job already exists"
        return 0
    fi
    
    print_info "Setting up automatic certificate renewal..."
    
    (crontab -l 2>/dev/null; echo "$cron_job") | crontab - 2>/dev/null || {
        print_warning "Could not add cron job"
        return 1
    }
    
    print_status "Auto-renewal cron job added (daily at 3 AM)"
}

generate_env() {
    if [ -f .env ]; then
        print_warning ".env file exists. Checking configuration..."
        source .env 2>/dev/null || true
        
        if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "$(grep '^DOMAIN=' .env 2>/dev/null | cut -d= -f2)" ]; then
            sed -i "s/^DOMAIN=.*/DOMAIN=${DOMAIN}/" .env
            print_info "Domain updated in .env"
        fi
        
        if ! grep -q "^POSTGRES_PASSWORD=" .env 2>/dev/null; then
            print_info "Adding PostgreSQL configuration..."
            local postgres_password
            postgres_password=$(generate_random 32)
            cat >> .env << EOF

# PostgreSQL Database (auto-generated)
POSTGRES_USER=panel
POSTGRES_PASSWORD=${postgres_password}
POSTGRES_DB=panel
EOF
            print_status "PostgreSQL configuration added"
        fi
        
        if [ -z "$PANEL_UID" ] || [ "$PANEL_UID" = "changeme" ]; then
            print_info "Regenerating credentials..."
        else
            print_status "Using existing configuration"
            return
        fi
    fi
    
    print_info "Generating .env configuration..."
    
    local panel_uid panel_password jwt_secret postgres_password
    panel_uid=$(generate_random 16)
    panel_password=$(generate_random 32)
    jwt_secret=$(generate_random 64)
    postgres_password=$(generate_random 32)
    
    cat > .env << EOF
# Domain (required for SSL)
DOMAIN=${DOMAIN}

# Panel Authentication (auto-generated)
PANEL_UID=${panel_uid}
PANEL_PASSWORD=${panel_password}

# JWT Settings
JWT_SECRET=${jwt_secret}
JWT_EXPIRE_MINUTES=1440

# Security
MAX_FAILED_ATTEMPTS=5
BAN_DURATION_SECONDS=900

# PostgreSQL Database (auto-generated)
POSTGRES_USER=panel
POSTGRES_PASSWORD=${postgres_password}
POSTGRES_DB=panel

# Ports
PANEL_PORT=443
PANEL_HTTP_PORT=80
EOF
    
    chmod 600 .env 2>/dev/null || true
    print_status ".env file generated"
}

generate_nginx_config() {
    print_info "Generating nginx configuration..."
    
    if [ -f "$SCRIPT_DIR/scripts/generate-nginx-config.sh" ]; then
        chmod +x "$SCRIPT_DIR/scripts/generate-nginx-config.sh" 2>/dev/null || true
        bash "$SCRIPT_DIR/scripts/generate-nginx-config.sh" "$SCRIPT_DIR"
        return
    fi
    
    if [ -z "$DOMAIN" ]; then
        print_error "DOMAIN variable is empty!"
        return 1
    fi
    
    if [ -z "$PANEL_UID" ]; then
        print_error "PANEL_UID variable is empty!"
        return 1
    fi
    
    export DOMAIN PANEL_UID
    envsubst '${DOMAIN} ${PANEL_UID}' < nginx/nginx.conf.template > nginx/nginx.conf
    
    print_status "nginx.conf generated for ${DOMAIN} with UID protection"
}

build_and_start() {
    print_info "Building and starting containers..."
    
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down --remove-orphans >/dev/null 2>&1 || true
    
    export DOCKER_BUILDKIT=1
    export COMPOSE_DOCKER_CLI_BUILD=1
    
    local max_retries=$MAX_RETRIES
    local retry=0
    local build_success=false
    local build_timeout="$TIMEOUT_DOCKER_BUILD"
    
    while [ $retry -lt $max_retries ]; do
        if ! check_docker_hub_quiet; then
            print_warning "Docker Hub is not accessible"
            if [ $retry -eq 0 ]; then
                fix_docker_network
            fi
        fi
        
        if [ -f .env ]; then
            export CACHE_BUST=$(md5sum .env 2>/dev/null | cut -d' ' -f1 || echo "nocache")
            print_info "Config hash: ${CACHE_BUST:0:8}... (rebuild on .env changes)"
        fi
        
        print_info "Building containers (attempt $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        
        local build_exit_code
        
        timeout "$build_timeout" docker compose build --build-arg CACHE_BUST=${CACHE_BUST:-} > "$BUILD_LOG" 2>&1 &
        local build_pid=$!
        
        while kill -0 $build_pid 2>/dev/null; do
            if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
                clear
                echo -e "${CYAN}[i]${NC} Building Docker images (attempt $((retry + 1))/$max_retries)... (press Ctrl+C to cancel)"
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                tail -30 "$BUILD_LOG" 2>/dev/null || true
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
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
            echo -e "${CYAN}║       Monitoring Panel Deployment          ║${NC}"
            echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
            echo ""
            print_status "Docker build completed successfully"
            break
        elif [ $build_exit_code -eq 124 ]; then
            print_error "Build timeout after ${build_timeout}s"
        else
            print_error "Build failed (exit code: $build_exit_code)"
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
            print_warning "Build failed, retrying after network fix..."
            fix_docker_network
            sleep "$RETRY_DELAY"
        fi
    done
    
    if [ "$build_success" = false ]; then
        print_error "Docker build failed after $max_retries attempts"
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
    
    print_info "Starting containers..."
    local up_output
    up_output=$(docker compose up -d 2>&1)
    local up_exit_code=$?
    
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
    
    source .env 2>/dev/null || true
    local max_attempts=30
    local attempt=0
    
    while [ $attempt -lt $max_attempts ]; do
        if timeout "$TIMEOUT_HEALTH_CHECK" curl -sk "https://${DOMAIN}/health" > /dev/null 2>&1; then
            print_status "Services are healthy"
            return 0
        fi
        if timeout "$TIMEOUT_HEALTH_CHECK" curl -sk "https://localhost/health" > /dev/null 2>&1; then
            print_status "Services are healthy"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 2
    done
    
    print_warning "Health check timed out, but services may still be starting"
}

print_credentials() {
    source .env 2>/dev/null || true
    
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
    
    safe_read "Press Enter to finish..." "" 30 >/dev/null
}

# ==================== Main ====================

main() {
    acquire_lock
    
    if [ "$EUID" -ne 0 ] && ! groups 2>/dev/null | grep -q docker; then
        print_error "Please run as root or add user to docker group"
        exit 1
    fi
    
    if ! check_docker; then
        if [ "$EUID" -eq 0 ]; then
            install_docker || exit 1
        else
            print_error "Docker not found. Please install Docker or run as root."
            exit 1
        fi
    fi
    
    prompt_domain
    
    if [ "$EUID" -eq 0 ]; then
        setup_firewall
    fi
    
    setup_ssl_certificate || exit 1
    setup_cert_renewal_cron
    
    generate_env
    source .env 2>/dev/null || true
    generate_nginx_config || exit 1
    
    build_and_start
    wait_for_health
    print_credentials
}

main "$@"
