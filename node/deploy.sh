#!/bin/bash
#
# Monitoring Node Agent - Auto Deploy Script
# Supports: Ubuntu 20.04+, Debian 11+
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Global flag: was native HAProxy running before installation
NATIVE_HAPROXY_WAS_RUNNING=false
NATIVE_HAPROXY_CONFIG_PATH=""
NATIVE_HAPROXY_CONFIG_CONTENT=""
NATIVE_HAPROXY_CONFIG_BACKUP="/tmp/haproxy-native-migration.cfg"

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Network Fix Functions ====================

check_docker_hub() {
    log_info "Checking Docker Hub availability..."
    
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        log_success "Docker Hub is accessible"
        return 0
    fi
    
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
        log_success "Docker Hub is accessible"
        return 0
    fi
    
    log_warn "Docker Hub is not accessible"
    return 1
}

disable_ipv6() {
    log_info "Disabling IPv6..."
    
    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true
    sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.lo.disable_ipv6=1 2>/dev/null || true
    
    log_success "IPv6 disabled"
}

configure_dns() {
    log_info "Configuring DNS (1.1.1.1, 8.8.8.8)..."
    
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
    
    systemctl daemon-reload 2>/dev/null || true
    systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
    
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
    log_info "Attempting to fix network issues..."
    
    echo ""
    log_info "Applying fix 1/3: IPv6"
    disable_ipv6
    
    echo ""
    log_info "Applying fix 2/3: DNS"
    configure_dns
    
    echo ""
    log_info "Applying fix 3/3: Docker mirrors"
    configure_docker_mirrors
    
    echo ""
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
    apt-get remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true

    # Install dependencies
    apt-get update
    apt-get install -y ca-certificates curl gnupg lsb-release

    # Add Docker GPG key
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$OS/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    # Add repository
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS \
        $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

    # Install Docker
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Start Docker
    systemctl start docker
    systemctl enable docker

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
    
    # Create renewal script
    cat > /opt/monitoring-node/renew-certs.sh << 'EOF'
#!/bin/bash
# Auto-renewal script for Let's Encrypt certificates
# Runs inside monitoring-api container where certbot is installed

# Check if container is running
if ! docker ps -q -f name=monitoring-api | grep -q .; then
    echo "monitoring-api container not running, skipping renewal"
    exit 0
fi

# Run certbot renew inside container
docker exec monitoring-api certbot renew --non-interactive --quiet

# Reload HAProxy to pick up new certificates (if running)
if docker ps -q -f name=monitoring-haproxy | grep -q .; then
    docker kill -s HUP monitoring-haproxy 2>/dev/null || true
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

# Check and migrate native HAProxy
migrate_native_haproxy() {
    log_info "Checking for native HAProxy installation..."
    
    # Check if native HAProxy is running (systemd or process)
    local haproxy_running=false
    local haproxy_enabled=false
    
    # Check systemd service
    if systemctl is-active --quiet haproxy 2>/dev/null; then
        haproxy_running=true
        log_info "Native HAProxy service is running (systemd)"
    fi
    
    # Check if enabled in systemd
    if systemctl is-enabled --quiet haproxy 2>/dev/null; then
        haproxy_enabled=true
    fi
    
    # Also check for process (in case it's running without systemd)
    if ! $haproxy_running && pgrep -x haproxy > /dev/null 2>&1; then
        haproxy_running=true
        log_info "Native HAProxy process is running"
    fi
    
    if ! $haproxy_running; then
        log_info "Native HAProxy is not running - container HAProxy will stay disabled"
        return 0
    fi
    
    # HAProxy is running - prepare for migration
    NATIVE_HAPROXY_WAS_RUNNING=true
    log_warn "Native HAProxy detected! Will migrate to container..."
    
    # Find and backup config BEFORE stopping HAProxy
    local config_paths=(
        "/etc/haproxy/haproxy.cfg"
        "/usr/local/etc/haproxy/haproxy.cfg"
    )
    
    for cfg in "${config_paths[@]}"; do
        if [ -f "$cfg" ]; then
            NATIVE_HAPROXY_CONFIG_PATH="$cfg"
            log_info "Found HAProxy config: $cfg"
            
            # CRITICAL: Read and save config content NOW before anything can change it
            NATIVE_HAPROXY_CONFIG_CONTENT=$(cat "$cfg" 2>/dev/null)
            
            if [ -n "$NATIVE_HAPROXY_CONFIG_CONTENT" ]; then
                # Save to backup file as well
                echo "$NATIVE_HAPROXY_CONFIG_CONTENT" > "$NATIVE_HAPROXY_CONFIG_BACKUP"
                chmod 644 "$NATIVE_HAPROXY_CONFIG_BACKUP"
                
                # Also create timestamped backup
                cp "$cfg" "/tmp/haproxy.cfg.backup.$(date +%Y%m%d_%H%M%S)"
                
                log_success "Config content saved ($(echo "$NATIVE_HAPROXY_CONFIG_CONTENT" | wc -l) lines)"
                log_info "Backup saved to: $NATIVE_HAPROXY_CONFIG_BACKUP"
            else
                log_warn "Config file exists but is empty!"
            fi
            break
        fi
    done
    
    if [ -z "$NATIVE_HAPROXY_CONFIG_CONTENT" ]; then
        log_warn "HAProxy config not found or empty"
        log_warn "Container will start with default config"
    fi
    
    # Stop native HAProxy
    log_info "Stopping native HAProxy..."
    
    if systemctl is-active --quiet haproxy 2>/dev/null; then
        systemctl stop haproxy
        log_success "HAProxy service stopped"
    fi
    
    # Kill any remaining haproxy processes
    pkill -x haproxy 2>/dev/null || true
    sleep 1
    
    # Verify it's stopped
    if pgrep -x haproxy > /dev/null 2>&1; then
        log_warn "Some HAProxy processes still running, force killing..."
        pkill -9 -x haproxy 2>/dev/null || true
        sleep 1
    fi
    
    # Disable native HAProxy autostart
    if $haproxy_enabled; then
        log_info "Disabling native HAProxy autostart..."
        systemctl disable haproxy 2>/dev/null || true
        log_success "Native HAProxy autostart disabled"
    fi
    
    log_success "Native HAProxy stopped and disabled"
    echo ""
    echo -e "${YELLOW}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║  Native HAProxy was running and has been stopped.              ║${NC}"
    echo -e "${YELLOW}║  Config will be migrated to container HAProxy.                 ║${NC}"
    echo -e "${YELLOW}║  Container HAProxy will be started automatically.              ║${NC}"
    echo -e "${YELLOW}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# Get the full Docker volume name for haproxy_config
get_haproxy_volume_name() {
    # Try multiple methods to find the volume
    local volume_name=""
    
    # Method 1: Use docker compose config to get exact volume name
    volume_name=$(docker compose config --volumes 2>/dev/null | grep haproxy_config | head -1)
    if [ -n "$volume_name" ]; then
        # docker compose config returns just the name, add project prefix
        local project_name
        project_name=$(docker compose config --format json 2>/dev/null | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4)
        if [ -n "$project_name" ]; then
            volume_name="${project_name}_haproxy_config"
        fi
    fi
    
    # Method 2: Search for volume containing haproxy_config
    if [ -z "$volume_name" ] || ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
        volume_name=$(docker volume ls -q --filter "name=haproxy_config" | head -1)
    fi
    
    # Method 3: Grep through all volumes
    if [ -z "$volume_name" ] || ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
        volume_name=$(docker volume ls -q | grep -E "(monitoring|node|server).*haproxy_config" | head -1)
    fi
    
    # Method 4: Just grep for haproxy_config
    if [ -z "$volume_name" ] || ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
        volume_name=$(docker volume ls -q | grep "haproxy_config" | head -1)
    fi
    
    echo "$volume_name"
}

# Copy saved HAProxy config content to container volume
copy_config_to_volume() {
    local config_content="$1"
    
    if [ -z "$config_content" ]; then
        log_warn "No config content to copy"
        return 1
    fi
    
    local volume_name
    volume_name=$(get_haproxy_volume_name)
    
    if [ -z "$volume_name" ]; then
        log_error "HAProxy config volume not found!"
        log_info "Available volumes:"
        docker volume ls
        return 1
    fi
    
    log_info "Found volume: $volume_name"
    log_info "Copying config to volume..."
    
    # Method 1: Try using docker cp via api container (most reliable)
    if docker ps -q -f name=monitoring-api | grep -q .; then
        log_info "Using monitoring-api container to copy config..."
        
        # Write config content to temp file in container
        echo "$config_content" | docker exec -i monitoring-api sh -c 'cat > /etc/haproxy/haproxy.cfg && chmod 644 /etc/haproxy/haproxy.cfg'
        
        if [ $? -eq 0 ]; then
            log_success "Config copied via monitoring-api container"
            return 0
        fi
        log_warn "Failed to copy via api container, trying alternative method..."
    fi
    
    # Method 2: Use temporary container with busybox (more common than alpine)
    local temp_file="/tmp/haproxy-temp-$$.cfg"
    echo "$config_content" > "$temp_file"
    
    # Try busybox first (smaller, often available), then alpine
    local copy_success=false
    
    for image in busybox alpine; do
        if docker run --rm \
            -v "$volume_name:/haproxy_config" \
            -v "$temp_file:/tmp/haproxy.cfg:ro" \
            "$image" sh -c "cp /tmp/haproxy.cfg /haproxy_config/haproxy.cfg && chmod 644 /haproxy_config/haproxy.cfg" 2>/dev/null; then
            copy_success=true
            log_success "Config copied using $image container"
            break
        fi
    done
    
    rm -f "$temp_file"
    
    if ! $copy_success; then
        log_error "Failed to copy config to volume"
        log_info "You may need to manually copy the config from: $NATIVE_HAPROXY_CONFIG_BACKUP"
        return 1
    fi
    
    return 0
}

# Start HAProxy container if native was running
start_haproxy_container_if_needed() {
    if ! $NATIVE_HAPROXY_WAS_RUNNING; then
        return 0
    fi
    
    log_info "Starting HAProxy container (migrating from native)..."
    
    # Use saved config content (not file path which may be stale)
    local config_to_copy=""
    
    # Try saved content first
    if [ -n "$NATIVE_HAPROXY_CONFIG_CONTENT" ]; then
        config_to_copy="$NATIVE_HAPROXY_CONFIG_CONTENT"
        log_info "Using saved config content from memory"
    # Fall back to backup file
    elif [ -f "$NATIVE_HAPROXY_CONFIG_BACKUP" ]; then
        config_to_copy=$(cat "$NATIVE_HAPROXY_CONFIG_BACKUP")
        log_info "Using saved config from backup file: $NATIVE_HAPROXY_CONFIG_BACKUP"
    # Last resort: try original path
    elif [ -n "$NATIVE_HAPROXY_CONFIG_PATH" ] && [ -f "$NATIVE_HAPROXY_CONFIG_PATH" ]; then
        config_to_copy=$(cat "$NATIVE_HAPROXY_CONFIG_PATH")
        log_warn "Using original config path (may have changed): $NATIVE_HAPROXY_CONFIG_PATH"
    fi
    
    if [ -n "$config_to_copy" ]; then
        # Wait a moment for volumes to be fully ready
        sleep 2
        
        copy_config_to_volume "$config_to_copy"
        
        if [ $? -ne 0 ]; then
            log_warn "Config migration failed, container will start with default config"
        fi
    else
        log_warn "No saved config found, container will use default config"
    fi
    
    # Start HAProxy container with profile
    log_info "Starting HAProxy container..."
    docker compose --profile haproxy up -d
    
    if [ $? -eq 0 ]; then
        # Wait for HAProxy to start
        log_info "Waiting for HAProxy to start..."
        sleep 3
        
        # Check if container is running
        if docker ps -q -f name=monitoring-haproxy | grep -q .; then
            log_success "HAProxy container is running"
            
            # Verify config was applied
            local container_config
            container_config=$(docker exec monitoring-haproxy cat /usr/local/etc/haproxy/haproxy.cfg 2>/dev/null | head -5)
            if [ -n "$container_config" ]; then
                log_success "HAProxy config verified in container"
            fi
        else
            log_warn "HAProxy container may have issues, checking logs..."
            docker logs monitoring-haproxy --tail 20 2>&1 || true
            log_warn "You can restart manually: docker compose --profile haproxy up -d"
        fi
    else
        log_error "Failed to start HAProxy container"
        log_warn "Check docker compose logs: docker compose logs haproxy"
        log_warn "You can start it manually: docker compose --profile haproxy up -d"
    fi
    
    # Cleanup backup file
    rm -f "$NATIVE_HAPROXY_CONFIG_BACKUP" 2>/dev/null || true
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
        apt-get update
        apt-get install -y ufw
    fi
    
    # Check if UFW was already active before we make changes
    local ufw_was_active=false
    if ufw status | grep -q "Status: active"; then
        ufw_was_active=true
    fi
    
    # Ask for panel IP
    ask_panel_ip
    
    # Remove old rule if exists (allow from anywhere)
    ufw delete allow 9100/tcp 2>/dev/null || true
    
    # Allow API port (9100) ONLY from panel IP
    log_info "Adding UFW rule: allow port 9100 from $PANEL_IP"
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp comment "Monitoring API from Panel" 2>/dev/null || \
    ufw allow from "$PANEL_IP" to any port 9100 proto tcp 2>/dev/null || true
    
    # Open port 80 for Let's Encrypt certificate verification
    ufw allow 80/tcp comment "HTTP for Let's Encrypt" 2>/dev/null || true
    
    # Allow SSH to avoid lockout (rule is added but only applied if UFW is active)
    ufw allow ssh 2>/dev/null || ufw allow 22/tcp 2>/dev/null || true
    
    # Only enable UFW if it was already active
    # If UFW was disabled, keep it disabled - rules are added but won't be applied
    if [ "$ufw_was_active" = true ]; then
        log_success "Firewall configured (UFW was active)"
    else
        log_warn "UFW is not active - rules added but firewall remains disabled"
        log_info "To enable firewall manually: ufw --force enable"
    fi
    
    log_info "Port 9100 accessible only from: $PANEL_IP"
    log_info "Ports 22 (SSH), 80 (HTTP) open for all"
}

# Build and start containers
start_containers() {
    log_info "Building and starting containers..."
    
    docker compose down 2>/dev/null || true
    
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
        
        log_info "Building containers (attempt $((retry + 1))/$max_retries)..."
        
        if docker compose build --no-cache 2>&1; then
            build_success=true
            break
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            log_warn "Build failed, retrying after network fix..."
            echo ""
            fix_docker_network
            echo ""
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
        echo ""
        exit 1
    fi
    
    docker compose up -d

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
        echo -n "."
        sleep 2
        attempt=$((attempt + 1))
    done

    echo ""
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

    # HAProxy API (HAProxy container disabled by default)
    echo -n "  /api/haproxy/status: "
    RESPONSE=$(curl -sk -H "X-API-Key: $API_KEY" "$BASE_URL/api/haproxy/status" 2>/dev/null)
    if echo "$RESPONSE" | grep -q '"running"'; then
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
    
    # HAProxy status
    if $NATIVE_HAPROXY_WAS_RUNNING; then
        echo -e "${GREEN}HAProxy Migration:${NC}"
        echo "  - Native HAProxy was detected and stopped"
        echo "  - Native HAProxy autostart disabled"
        if [ -n "$NATIVE_HAPROXY_CONFIG_PATH" ]; then
            echo "  - Config migrated from: $NATIVE_HAPROXY_CONFIG_PATH"
        fi
        if docker ps -q -f name=monitoring-haproxy | grep -q .; then
            echo -e "  - Container HAProxy: ${GREEN}Running${NC}"
        else
            echo -e "  - Container HAProxy: ${YELLOW}Not running (check logs)${NC}"
            echo "    Check logs: docker logs monitoring-haproxy"
        fi
    else
        echo -e "${YELLOW}NOTE: HAProxy is DISABLED by default.${NC}"
        echo "Enable HAProxy from the panel or manually:"
        echo "  docker compose --profile haproxy up -d"
    fi
    echo ""
    echo "Container status:"
    docker compose ps
    echo ""
    echo "Commands:"
    echo "  docker compose logs -f                    # View logs"
    echo "  docker compose restart                    # Restart all"
    echo "  docker compose down                       # Stop all"
    echo "  docker compose --profile haproxy up -d    # Enable HAProxy"
    echo "  docker compose --profile haproxy down     # Disable HAProxy"
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
    migrate_native_haproxy      # Check and stop native HAProxy before Docker
    install_docker
    setup_firewall
    setup_env
    setup_ssl
    setup_cert_renewal_cron
    start_containers
    wait_for_services
    start_haproxy_container_if_needed  # Start HAProxy container if native was running
    check_endpoints
    show_status
}

# Run
cd "$(dirname "$0")"
main "$@"
