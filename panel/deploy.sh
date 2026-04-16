#!/bin/bash
#
# Monitoring Panel - Auto Deploy Script
#

# ==================== Safety Settings ====================

set +e  # Handle errors manually

# Prevent interactive prompts during package installation
# needrestart on Ubuntu 22.04+ shows ncurses dialog that hangs scripts
# and can restart sshd, killing the SSH session
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

LOCKFILE="/tmp/monitoring-panel-deploy.lock"
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

# ==================== Configuration ====================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

CERT_RENEWAL_DAYS=30
SSL_METHOD=""
CF_API_TOKEN=""
CF_DOMAINS=""
CF_CREDENTIALS_FILE="/etc/letsencrypt/cloudflare.ini"

echo ""
echo -e "${CYAN}══ Monitoring Panel Deployment ══${NC}"
echo ""

# ==================== Core Functions ====================

check_docker() {
    if command -v docker &> /dev/null; then
        print_status "Docker is installed"
        return 0
    fi
    return 1
}

wait_for_apt_lock() {
    local max_wait=120
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
        if [ $waited -eq 0 ]; then
            print_warning "Waiting for apt lock..."
        fi
        sleep 3
        waited=$((waited + 3))
        if [ $waited -ge $max_wait ]; then
            print_warning "apt lock wait timeout (${max_wait}s), trying anyway..."
            return 0
        fi
    done
    return 0
}

install_docker() {
    print_info "Installing Docker..."
    suppress_needrestart
    wait_for_apt_lock

    if [ -f /etc/debian_version ]; then
        local os_id os_codename
        os_id=$(. /etc/os-release && echo "$ID")
        os_codename=$(. /etc/os-release && echo "$VERSION_CODENAME")

        spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
            env DEBIAN_FRONTEND=noninteractive \
            apt-get update -qq || print_warning "apt update had issues"

        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Docker dependencies" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            ca-certificates curl gnupg || {
            print_error "Failed to install dependencies"
            return 1
        }

        install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true

        if ! spin "Downloading Docker GPG key" bash -c \
            "curl -fsSL 'https://download.docker.com/linux/${os_id}/gpg' | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null"; then
            print_error "Failed to download Docker GPG key"
            return 1
        fi
        chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true

        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${os_id} \
          ${os_codename} stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

        spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists (Docker repo)" \
            env DEBIAN_FRONTEND=noninteractive \
            apt-get update -qq || print_warning "apt update had issues"

        suppress_needrestart
        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Docker Engine" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            docker-ce docker-ce-cli containerd.io docker-compose-plugin || {
            print_error "Failed to install Docker"
            return 1
        }
    elif [ -f /etc/redhat-release ]; then
        spin "Installing yum-utils" yum install -y -q yum-utils || return 1
        yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
        spin "Installing Docker (yum)" yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin || return 1
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
    echo -e "${YELLOW}══ Domain Configuration ══${NC}"
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

prompt_ssl_method() {
    # Извлекаем базовый домен: mon.example.com → example.com
    local base_domain
    base_domain=$(echo "$DOMAIN" | awk -F. '{print $(NF-1)"."$NF}')

    echo ""
    echo -e "${YELLOW}══ SSL Certificate ══${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} Сертификат на ${CYAN}${DOMAIN}${NC} — Let's Encrypt, нужен открытый порт 80"
    echo -e "  ${GREEN}2)${NC} Wildcard ${CYAN}*.${base_domain}${NC} — Cloudflare DNS API, порт 80 не нужен"
    echo ""

    local choice
    choice=$(safe_read "Выберите (1/2): " "1" 60)

    case "$choice" in
        2)
            SSL_METHOD="cloudflare"
            CF_DOMAINS="*.${base_domain}"
            print_status "SSL: wildcard *.${base_domain} (Cloudflare DNS)"
            prompt_cloudflare_token
            ;;
        *)
            SSL_METHOD="letsencrypt"
            print_status "SSL: ${DOMAIN} (Let's Encrypt)"
            ;;
    esac
}

prompt_cloudflare_token() {
    echo ""
    echo -e "Создайте API Token: ${CYAN}Cloudflare Dashboard → Profile → API Tokens → Create Token${NC}"
    echo -e "  Permissions: ${GREEN}Zone / DNS / Edit${NC}"
    echo -e "  Zone Resources: ${GREEN}Include / All zones${NC} (или конкретная зона)"
    echo ""

    local max_attempts=3
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        CF_API_TOKEN=$(safe_read "Cloudflare API Token: " "" 120)

        if [ -z "$CF_API_TOKEN" ]; then
            print_error "API Token обязателен"
            attempt=$((attempt + 1))
            continue
        fi

        if [ ${#CF_API_TOKEN} -lt 30 ]; then
            print_error "Token слишком короткий"
            attempt=$((attempt + 1))
            continue
        fi

        break
    done

    if [ -z "$CF_API_TOKEN" ]; then
        print_error "Не удалось получить API Token"
        exit 1
    fi
}

verify_cloudflare_token() {
    print_info "Проверка Cloudflare API Token..."

    local response
    response=$(timeout "$TIMEOUT_CURL" curl -fsSL --connect-timeout 10 --max-time "$TIMEOUT_CURL" \
        -H "Authorization: Bearer ${CF_API_TOKEN}" \
        -H "Content-Type: application/json" \
        "https://api.cloudflare.com/client/v4/user/tokens/verify" 2>/dev/null)

    if echo "$response" | grep -q '"success":true'; then
        print_status "Cloudflare API Token валиден"
        return 0
    fi

    print_error "Cloudflare API Token невалиден или нет доступа"
    echo -e "  ${RED}Ответ: ${response}${NC}"
    return 1
}

save_cloudflare_credentials() {
    mkdir -p "$(dirname "$CF_CREDENTIALS_FILE")" 2>/dev/null || true

    cat > "$CF_CREDENTIALS_FILE" << EOF
dns_cloudflare_api_token = ${CF_API_TOKEN}
EOF

    chmod 600 "$CF_CREDENTIALS_FILE"
    print_status "Cloudflare credentials сохранены"
}

install_certbot_dns_cloudflare() {
    if command -v certbot &> /dev/null && python3 -c "import certbot_dns_cloudflare" 2>/dev/null; then
        print_status "Certbot + DNS Cloudflare plugin уже установлены"
        return 0
    fi

    print_info "Установка Certbot + DNS Cloudflare plugin..."
    suppress_needrestart
    wait_for_apt_lock

    if [ -f /etc/debian_version ]; then
        spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
            env DEBIAN_FRONTEND=noninteractive \
            apt-get update -qq || true

        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Certbot + Cloudflare plugin" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            certbot python3-certbot-dns-cloudflare || {
            print_error "Не удалось установить certbot/cloudflare plugin"
            return 1
        }
    elif [ -f /etc/redhat-release ]; then
        if command -v dnf &> /dev/null; then
            spin "Installing Certbot + Cloudflare (dnf)" dnf install -y -q certbot python3-certbot-dns-cloudflare || return 1
        else
            spin "Installing Certbot + Cloudflare (yum)" yum install -y -q certbot python3-certbot-dns-cloudflare || return 1
        fi
    else
        print_error "Unsupported OS for automatic Certbot installation"
        return 1
    fi

    print_status "Certbot + Cloudflare plugin установлены"
}

obtain_certificate_cloudflare() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"

    print_info "Получение сертификата через Cloudflare DNS API..."

    local domain_args=""
    for d in $CF_DOMAINS; do
        domain_args="$domain_args -d $d"
    done

    if timeout "$TIMEOUT_CERTBOT" certbot certonly \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CF_CREDENTIALS_FILE" \
        --dns-cloudflare-propagation-seconds 30 \
        --non-interactive \
        --agree-tos \
        --register-unsafely-without-email \
        --cert-name "$DOMAIN" \
        $domain_args 2>&1; then
        print_status "Сертификат получен!"
        return 0
    fi

    print_error "Не удалось получить сертификат"
    print_info "Проверьте:"
    echo "  1. API Token имеет права Zone / DNS / Edit"
    echo "  2. Домены принадлежат вашему Cloudflare аккаунту"
    echo "  3. DNS зона активна в Cloudflare"
    return 1
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

    echo ""
}

resolve_domain_ip() {
    local domain="$1"
    local ip=""

    if command -v dig &>/dev/null; then
        ip=$(dig +short "$domain" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    fi
    if [ -z "$ip" ] && command -v host &>/dev/null; then
        ip=$(host "$domain" 2>/dev/null | awk '/has address/ {print $4}' | head -1)
    fi
    if [ -z "$ip" ] && command -v nslookup &>/dev/null; then
        ip=$(nslookup "$domain" 2>/dev/null | awk '/^Address:/ && !/127\.0\.0/ {print $2}' | tail -1)
    fi
    if [ -z "$ip" ] && command -v getent &>/dev/null; then
        ip=$(getent ahosts "$domain" 2>/dev/null | awk '/STREAM/ {print $1}' | head -1)
    fi

    echo "$ip"
}

verify_domain_dns() {
    print_info "Verifying DNS for ${DOMAIN}..."

    local server_ip domain_ip

    server_ip=$(get_server_ip)
    if [ -z "$server_ip" ]; then
        print_warning "Could not detect server public IP — skipping DNS check"
        return 0
    fi
    print_info "Server IP: ${server_ip}"

    domain_ip=$(resolve_domain_ip "$DOMAIN")
    if [ -z "$domain_ip" ]; then
        echo ""
        print_error "Domain ${DOMAIN} does not resolve to any IP address"
        print_info "Create DNS A-record: ${DOMAIN} → ${server_ip}"
        echo ""
        local choice
        choice=$(safe_read "Continue anyway? / Продолжить? (y/N): " "n" 30)
        if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
            print_warning "Continuing without DNS verification"
            return 0
        fi
        print_info "Installation cancelled. Fix DNS and try again."
        return 1
    fi

    print_info "Domain IP: ${domain_ip}"

    if [ "$domain_ip" = "$server_ip" ]; then
        print_status "DNS OK: ${DOMAIN} → ${server_ip}"
        return 0
    fi

    echo ""
    print_error "DNS mismatch!"
    echo -e "    ${YELLOW}Domain ${DOMAIN}${NC} resolves to: ${RED}${domain_ip}${NC}"
    echo -e "    ${YELLOW}This server IP:${NC}             ${GREEN}${server_ip}${NC}"
    echo ""
    print_info "Fix DNS A-record: ${DOMAIN} → ${server_ip}"
    echo ""
    local choice
    choice=$(safe_read "Continue anyway? / Продолжить? (y/N): " "n" 30)
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
        print_warning "Continuing with DNS mismatch — SSL may fail!"
        return 0
    fi
    print_info "Installation cancelled. Fix DNS and try again."
    return 1
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
    suppress_needrestart
    wait_for_apt_lock

    if [ -f /etc/debian_version ]; then
        spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
            env DEBIAN_FRONTEND=noninteractive \
            apt-get update -qq || true
        spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing Certbot" \
            env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
            apt-get install -y -qq \
            -o Dpkg::Options::="--force-confold" \
            -o Dpkg::Options::="--force-confdef" \
            certbot || {
            print_error "Failed to install certbot"
            return 1
        }
    elif [ -f /etc/redhat-release ]; then
        if command -v dnf &> /dev/null; then
            spin "Installing Certbot (dnf)" dnf install -y -q certbot || return 1
        else
            spin "Installing Certbot (yum)" yum install -y -q certbot || return 1
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

find_existing_cert() {
    local domain="$1"
    local live_dir="/etc/letsencrypt/live"

    [ -d "$live_dir" ] || return 1

    for cert_dir in "${live_dir}"/*/; do
        [ -d "$cert_dir" ] || continue
        [ -f "${cert_dir}fullchain.pem" ] || continue
        [ -f "${cert_dir}privkey.pem" ] || continue

        local sans
        sans=$(openssl x509 -in "${cert_dir}fullchain.pem" -noout -text 2>/dev/null \
            | grep -A1 "Subject Alternative Name" \
            | tail -1 \
            | grep -oE 'DNS:[^ ,]+' \
            | sed 's/DNS://g')

        for san in $sans; do
            [ "$san" = "$domain" ] && { echo "${cert_dir%/}"; return 0; }

            # *.example.com covers sub.example.com (one level)
            if [[ "$san" == \*.* ]]; then
                local wildcard_base="${san#\*.}"
                local domain_parent="${domain#*.}"
                if [[ "$domain" == *.* ]] && [ "$domain_parent" = "$wildcard_base" ]; then
                    echo "${cert_dir%/}"
                    return 0
                fi
            fi
        done
    done

    return 1
}

setup_ssl_certificate() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"

    # 1) Cert already exists at expected path (directly or via symlink from previous run)
    if [ -f "${cert_path}/fullchain.pem" ] && [ -f "${cert_path}/privkey.pem" ]; then
        local days_remaining
        days_remaining=$(get_cert_days_remaining)

        # Symlinked cert (wildcard/external) — don't touch, just report
        if [ -L "$cert_path" ]; then
            local real_path
            real_path=$(readlink -f "$cert_path" 2>/dev/null || readlink "$cert_path")
            if [ "$days_remaining" -gt 0 ]; then
                print_status "Using linked certificate: $(basename "$real_path") (valid for ${days_remaining} days)"
            elif [ "$days_remaining" -le 0 ] && [ "$days_remaining" -ne -1 ]; then
                print_error "Linked certificate has expired! Renew source: ${real_path}"
                return 1
            else
                print_warning "Cannot read expiration for linked certificate: ${real_path}"
            fi
            return 0
        fi

        # Direct cert — check expiry, offer renewal
        if [ "$days_remaining" -lt 0 ]; then
            print_warning "Certificate exists but cannot read expiration date"
            print_info "Attempting to renew..."
            renew_certificate_auto || return 1
        elif [ "$days_remaining" -le 0 ]; then
            print_error "Certificate has EXPIRED!"
            print_info "Renewing certificate..."
            renew_certificate_auto || return 1
        elif [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
            print_warning "Certificate expires in ${days_remaining} days"
            echo ""
            local renew_choice
            renew_choice=$(safe_read "Renew certificate now? (Y/n): " "Y" 30)
            if [ "$renew_choice" != "n" ] && [ "$renew_choice" != "N" ]; then
                renew_certificate_auto || return 1
            else
                print_info "Skipping renewal. Certificate valid for ${days_remaining} days."
            fi
        else
            print_status "Certificate valid for ${days_remaining} days"
        fi

        local final_days
        final_days=$(get_cert_days_remaining)
        if [ "$final_days" -gt 0 ]; then
            print_status "SSL certificate ready (expires in ${final_days} days)"
        fi
        return 0
    fi

    # 2) Cert not at expected path — search for wildcard/SAN match in letsencrypt
    local found_cert
    if found_cert=$(find_existing_cert "$DOMAIN"); then
        # Remove empty dir if exists (prevents symlink failure)
        if [ -d "$cert_path" ] && [ ! -L "$cert_path" ]; then
            rmdir "$cert_path" 2>/dev/null || true
        fi

        if [ ! -e "$cert_path" ]; then
            ln -s "$found_cert" "$cert_path" 2>/dev/null || {
                print_error "Failed to create symlink: ${cert_path} → ${found_cert}"
                return 1
            }
        fi

        local days_remaining
        days_remaining=$(get_cert_days_remaining)
        if [ "$days_remaining" -gt 0 ]; then
            print_status "Found matching certificate: $(basename "$found_cert") (valid for ${days_remaining} days)"
        else
            print_warning "Found matching certificate: $(basename "$found_cert"), but it may be expired"
        fi
        return 0
    fi

    # 3) No existing cert — obtain new
    print_info "No certificate found for ${DOMAIN}"

    if [ "$SSL_METHOD" = "cloudflare" ]; then
        verify_cloudflare_token || return 1
        save_cloudflare_credentials
        install_certbot_dns_cloudflare || return 1
        obtain_certificate_cloudflare || return 1
    else
        install_certbot || return 1
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

renew_certificate_auto() {
    # Определяем метод по наличию cloudflare credentials
    if [ -f "$CF_CREDENTIALS_FILE" ]; then
        install_certbot_dns_cloudflare || return 1
        print_info "Renewing certificate via Cloudflare DNS..."
        if timeout "$TIMEOUT_CERTBOT" certbot renew --cert-name "$DOMAIN" --non-interactive 2>&1; then
            print_status "Certificate renewed successfully!"
            return 0
        fi
        print_error "Failed to renew certificate"
        return 1
    fi

    install_certbot || return 1
    renew_certificate
}

setup_cert_renewal_cron() {
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"

    # Symlinked cert — renewal is managed by the source certificate owner
    if [ -L "$cert_path" ]; then
        print_info "Certificate is linked — auto-renewal managed externally"
        return 0
    fi

    local deploy_hook="docker compose -f ${SCRIPT_DIR}/docker-compose.yml restart nginx"
    local cron_job

    if [ "$SSL_METHOD" = "cloudflare" ] || [ -f "$CF_CREDENTIALS_FILE" ]; then
        # Cloudflare: certbot renew сам подхватит dns-cloudflare из renewal config
        cron_job="0 3 * * * certbot renew --quiet --deploy-hook '${deploy_hook}'"
    else
        # Let's Encrypt standalone: нужно остановить nginx для порта 80
        cron_job="0 3 * * * certbot renew --quiet --deploy-hook '${deploy_hook}'"
    fi

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

# SSL Method (letsencrypt or cloudflare)
SSL_METHOD=${SSL_METHOD}

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

pull_and_start() {
    print_info "Pulling and starting containers..."

    spin "Stopping old containers" \
        timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down --remove-orphans 2>/dev/null || true

    # Pull ready images from GHCR (normal flow)
    if ! spin_retry 240 5 10 "Pulling Docker images" docker compose pull 2>/dev/null; then
        print_warning "Failed to pull from registry, building locally..."
        spin "Pulling base images" bash -c \
            'docker compose pull --ignore-buildable 2>/dev/null || true'
        spin_retry 600 2 10 "Building images from source" docker compose build || {
            print_error "Failed to build images"
            exit 1
        }
    fi

    spin "Starting containers" docker compose up -d || {
        print_error "Failed to start containers"
        exit 1
    }
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
    local cert_path="/etc/letsencrypt/live/${DOMAIN}"
    local is_linked=false
    [ -L "$cert_path" ] && is_linked=true

    local ssl_label="Let's Encrypt (HTTP-01)"
    if [ -f "$CF_CREDENTIALS_FILE" ]; then
        ssl_label="Cloudflare DNS API"
    fi

    echo -e "${CYAN}SSL Certificate:${NC} ${ssl_label}"
    if [ "$days_remaining" -gt 0 ]; then
        if [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
            echo -e "  ${YELLOW}Expires in ${days_remaining} days (renewal recommended)${NC}"
        else
            echo -e "  ${GREEN}Valid for ${days_remaining} days${NC}"
        fi
    else
        echo -e "  ${RED}Check certificate status${NC}"
    fi
    if [ "$is_linked" = true ]; then
        local real_path
        real_path=$(readlink -f "$cert_path" 2>/dev/null || readlink "$cert_path")
        echo -e "  Source: ${CYAN}${real_path}${NC}"
        echo -e "  Auto-renewal: Managed externally"
    else
        echo -e "  Auto-renewal: Enabled (cron daily at 3 AM)"
    fi
    echo ""
    
    echo ""
    echo -e "  ${GREEN}══ ДАННЫЕ ДЛЯ ВХОДА В ПАНЕЛЬ ══${NC}"
    echo ""
    echo -e "    ${YELLOW}URL панели:${NC}"
    echo -e "    ${CYAN}https://${DOMAIN}/${PANEL_UID}${NC}"
    echo ""
    echo -e "    ${YELLOW}Пароль:${NC}"
    echo -e "    ${CYAN}${PANEL_PASSWORD}${NC}"
    echo ""
    echo -e "  ${RED}ВАЖНО: Сохраните эти данные!${NC}"
    echo -e "  ${RED}После закрытия они не будут показаны снова.${NC}"
    echo ""
    
    safe_read "Press Enter to finish..." "" 7200 >/dev/null
}

# ==================== Main ====================

main() {
    acquire_lock
    load_proxy
    configure_apt_proxy
    
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
    configure_docker_proxy
    
    prompt_domain
    prompt_ssl_method

    if [ "$SSL_METHOD" = "letsencrypt" ]; then
        verify_domain_dns || exit 1
    fi

    if [ "$EUID" -eq 0 ]; then
        setup_firewall
    fi

    setup_ssl_certificate || exit 1
    setup_cert_renewal_cron
    
    generate_env
    source .env 2>/dev/null || true
    generate_nginx_config || exit 1
    
    pull_and_start
    wait_for_health
    print_credentials
}

main "$@"
