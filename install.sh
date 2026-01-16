#!/bin/bash
#
# Monitoring System Installer
# 
# Quick install:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
#
# After installation, run: monitoring
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# GitHub repo
REPO_URL="https://github.com/Joliz1337/monitoring.git"
TMP_DIR="/tmp/monitoring-installer-$$"

# Install paths
PANEL_DIR="/opt/monitoring-panel"
NODE_DIR="/opt/monitoring-node"
BIN_PATH="/usr/local/bin/monitoring"

# Language (default: auto-detect or English)
LANG_CODE="en"

# Server country (auto-detect)
SERVER_COUNTRY=""

# GitHub mirrors for Russia
# Format: "type:base_url" where type is 'proxy' (prepend full URL) or 'replace' (replace github.com)
GITHUB_MIRRORS_RU=(
    "replace:https://kkgithub.com"
    "replace:https://hub.gitmirror.com"
    "proxy:https://ghproxy.com"
    "proxy:https://gh-proxy.com"
    "direct:https://github.com"
)

# GitHub mirrors for other countries
GITHUB_MIRRORS_OTHER=(
    "direct:https://github.com"
)

# Current active mirror (format: "type:base_url")
ACTIVE_GITHUB_MIRROR=""

# Docker mirror list
DOCKER_MIRRORS=(
    "https://mirror.gcr.io"
    "https://registry.docker-cn.com"
    "https://docker.mirrors.ustc.edu.cn"
)

# ==================== Translations ====================

declare -A MSG_EN
declare -A MSG_RU

# English messages
MSG_EN[select_language]="Select language / Выберите язык:"
MSG_EN[installing_git]="Installing git..."
MSG_EN[downloading_repo]="Downloading repository..."
MSG_EN[repo_downloaded]="Repository downloaded"
MSG_EN[menu_title]="Monitoring System Installer"
MSG_EN[menu_install_panel]="Install panel"
MSG_EN[menu_install_node]="Install node"
MSG_EN[menu_update_panel]="Update panel"
MSG_EN[menu_update_node]="Update node"
MSG_EN[menu_remove_panel]="Remove panel"
MSG_EN[menu_remove_node]="Remove node"
MSG_EN[menu_exit]="Exit"
MSG_EN[status]="Status"
MSG_EN[installed]="installed"
MSG_EN[not_installed]="not installed"
MSG_EN[select_action]="Select action"
MSG_EN[invalid_option]="Invalid option"
MSG_EN[goodbye]="Goodbye!"
MSG_EN[press_enter]="Press Enter to continue..."
MSG_EN[panel_already_installed]="Panel already installed at"
MSG_EN[node_already_installed]="Node already installed at"
MSG_EN[reinstall_confirm]="Reinstall? This will remove existing data! (y/N)"
MSG_EN[installation_cancelled]="Installation cancelled"
MSG_EN[installing_panel]="Installing panel to"
MSG_EN[installing_node]="Installing node to"
MSG_EN[panel_installed]="Panel installed successfully!"
MSG_EN[node_installed]="Node installed successfully!"
MSG_EN[panel_not_found]="Panel not found at"
MSG_EN[node_not_found]="Node not found at"
MSG_EN[remove_confirm]="Remove and delete all data? (y/N)"
MSG_EN[removal_cancelled]="Removal cancelled"
MSG_EN[stopping_containers]="Stopping containers..."
MSG_EN[removing_files]="Removing files..."
MSG_EN[panel_removed]="Panel removed"
MSG_EN[node_removed]="Node removed"
MSG_EN[updating_panel]="Updating panel..."
MSG_EN[updating_node]="Updating node..."
MSG_EN[update_complete]="Update complete!"
MSG_EN[run_as_root]="Please run as root: sudo bash install.sh"
MSG_EN[cli_installed]="Command 'monitoring' installed. Run it anytime to manage your installation."
MSG_EN[run_monitoring]="You can now run: monitoring"
MSG_EN[menu_optimize_system]="System optimizations (BBR, sysctl, limits)"
MSG_EN[optimizing_system]="Applying system optimizations..."
MSG_EN[optimizations_applied]="System optimizations applied!"
MSG_EN[optimizations_status]="Optimizations"
MSG_EN[applied]="applied"
MSG_EN[not_applied]="not applied"

# Geo-detection messages - English
MSG_EN[detecting_country]="Detecting server location..."
MSG_EN[country_detected]="Server location"
MSG_EN[country_russia]="Russia - using GitHub mirrors"
MSG_EN[country_other]="using direct GitHub"
MSG_EN[country_detection_failed]="Could not detect location, using direct GitHub"
MSG_EN[testing_mirrors]="Testing GitHub mirrors speed..."
MSG_EN[mirror_selected]="Selected mirror"
MSG_EN[mirror_speed]="Speed"
MSG_EN[mirror_failed]="Mirror unavailable"
MSG_EN[all_mirrors_failed]="All mirrors failed, trying direct GitHub"
MSG_EN[download_slow]="Download is slow, trying next mirror..."
MSG_EN[download_timeout]="Download timeout, trying next mirror..."

# Network/Docker messages - English
MSG_EN[checking_docker_network]="Checking Docker Hub availability..."
MSG_EN[docker_network_ok]="Docker Hub is accessible"
MSG_EN[docker_network_error]="Docker Hub is not accessible"
MSG_EN[fixing_docker_network]="Attempting to fix network issues..."
MSG_EN[disabling_ipv6]="Disabling IPv6..."
MSG_EN[ipv6_disabled]="IPv6 disabled"
MSG_EN[configuring_dns]="Configuring DNS (1.1.1.1, 8.8.8.8)..."
MSG_EN[dns_configured]="DNS configured"
MSG_EN[configuring_mirrors]="Configuring Docker registry mirrors..."
MSG_EN[mirrors_configured]="Docker mirrors configured"
MSG_EN[restarting_docker]="Restarting Docker service..."
MSG_EN[docker_restarted]="Docker service restarted"
MSG_EN[build_failed]="Docker build failed"
MSG_EN[retrying_build]="Retrying build after network fix..."
MSG_EN[build_success]="Docker build completed successfully"
MSG_EN[network_fix_failed]="Could not fix network issues automatically"
MSG_EN[manual_fix_hint]="Try manually: check firewall, DNS settings, or use VPN"
MSG_EN[checking_connectivity]="Checking network connectivity..."
MSG_EN[connectivity_ok]="Network connectivity OK"
MSG_EN[connectivity_failed]="Network connectivity failed"
MSG_EN[applying_fix]="Applying fix"

# Russian messages
MSG_RU[select_language]="Select language / Выберите язык:"
MSG_RU[installing_git]="Установка git..."
MSG_RU[downloading_repo]="Скачивание репозитория..."
MSG_RU[repo_downloaded]="Репозиторий скачан"
MSG_RU[menu_title]="Установщик системы мониторинга"
MSG_RU[menu_install_panel]="Установить панель"
MSG_RU[menu_install_node]="Установить ноду"
MSG_RU[menu_update_panel]="Обновить панель"
MSG_RU[menu_update_node]="Обновить ноду"
MSG_RU[menu_remove_panel]="Удалить панель"
MSG_RU[menu_remove_node]="Удалить ноду"
MSG_RU[menu_exit]="Выход"
MSG_RU[status]="Статус"
MSG_RU[installed]="установлена"
MSG_RU[not_installed]="не установлена"
MSG_RU[select_action]="Выберите действие"
MSG_RU[invalid_option]="Неверный выбор"
MSG_RU[goodbye]="До свидания!"
MSG_RU[press_enter]="Нажмите Enter для продолжения..."
MSG_RU[panel_already_installed]="Панель уже установлена в"
MSG_RU[node_already_installed]="Нода уже установлена в"
MSG_RU[reinstall_confirm]="Переустановить? Все данные будут удалены! (y/N)"
MSG_RU[installation_cancelled]="Установка отменена"
MSG_RU[installing_panel]="Установка панели в"
MSG_RU[installing_node]="Установка ноды в"
MSG_RU[panel_installed]="Панель успешно установлена!"
MSG_RU[node_installed]="Нода успешно установлена!"
MSG_RU[panel_not_found]="Панель не найдена в"
MSG_RU[node_not_found]="Нода не найдена в"
MSG_RU[remove_confirm]="Удалить вместе со всеми данными? (y/N)"
MSG_RU[removal_cancelled]="Удаление отменено"
MSG_RU[stopping_containers]="Остановка контейнеров..."
MSG_RU[removing_files]="Удаление файлов..."
MSG_RU[panel_removed]="Панель удалена"
MSG_RU[node_removed]="Нода удалена"
MSG_RU[updating_panel]="Обновление панели..."
MSG_RU[updating_node]="Обновление ноды..."
MSG_RU[update_complete]="Обновление завершено!"
MSG_RU[run_as_root]="Запустите от root: sudo bash install.sh"
MSG_RU[cli_installed]="Команда 'monitoring' установлена. Используйте её для управления установкой."
MSG_RU[run_monitoring]="Теперь можно запускать: monitoring"
MSG_RU[menu_optimize_system]="Системные оптимизации (BBR, sysctl, limits)"
MSG_RU[optimizing_system]="Применение системных оптимизаций..."
MSG_RU[optimizations_applied]="Системные оптимизации применены!"
MSG_RU[optimizations_status]="Оптимизации"
MSG_RU[applied]="применены"
MSG_RU[not_applied]="не применены"

# Geo-detection messages - Russian
MSG_RU[detecting_country]="Определение местоположения сервера..."
MSG_RU[country_detected]="Местоположение сервера"
MSG_RU[country_russia]="Россия - используются зеркала GitHub"
MSG_RU[country_other]="используется прямой GitHub"
MSG_RU[country_detection_failed]="Не удалось определить местоположение, используется прямой GitHub"
MSG_RU[testing_mirrors]="Тестирование скорости зеркал GitHub..."
MSG_RU[mirror_selected]="Выбрано зеркало"
MSG_RU[mirror_speed]="Скорость"
MSG_RU[mirror_failed]="Зеркало недоступно"
MSG_RU[all_mirrors_failed]="Все зеркала недоступны, пробуем прямой GitHub"
MSG_RU[download_slow]="Медленная загрузка, пробуем следующее зеркало..."
MSG_RU[download_timeout]="Таймаут загрузки, пробуем следующее зеркало..."

# Network/Docker messages - Russian
MSG_RU[checking_docker_network]="Проверка доступности Docker Hub..."
MSG_RU[docker_network_ok]="Docker Hub доступен"
MSG_RU[docker_network_error]="Docker Hub недоступен"
MSG_RU[fixing_docker_network]="Попытка исправить сетевые проблемы..."
MSG_RU[disabling_ipv6]="Отключение IPv6..."
MSG_RU[ipv6_disabled]="IPv6 отключён"
MSG_RU[configuring_dns]="Настройка DNS (1.1.1.1, 8.8.8.8)..."
MSG_RU[dns_configured]="DNS настроен"
MSG_RU[configuring_mirrors]="Настройка зеркал Docker registry..."
MSG_RU[mirrors_configured]="Зеркала Docker настроены"
MSG_RU[restarting_docker]="Перезапуск Docker..."
MSG_RU[docker_restarted]="Docker перезапущен"
MSG_RU[build_failed]="Сборка Docker образов не удалась"
MSG_RU[retrying_build]="Повторная попытка сборки после исправления сети..."
MSG_RU[build_success]="Сборка Docker образов завершена успешно"
MSG_RU[network_fix_failed]="Не удалось автоматически исправить сетевые проблемы"
MSG_RU[manual_fix_hint]="Попробуйте вручную: проверьте firewall, DNS или используйте VPN"
MSG_RU[checking_connectivity]="Проверка сетевого подключения..."
MSG_RU[connectivity_ok]="Сетевое подключение в порядке"
MSG_RU[connectivity_failed]="Сетевое подключение не работает"
MSG_RU[applying_fix]="Применяется исправление"

# Get message in current language
msg() {
    local key="$1"
    if [ "$LANG_CODE" = "ru" ]; then
        echo "${MSG_RU[$key]}"
    else
        echo "${MSG_EN[$key]}"
    fi
}

# ==================== Logging ====================

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Geo Detection & GitHub Mirrors ====================

# Detect server country using IP geolocation APIs
detect_country() {
    log_info "$(msg detecting_country)"
    
    local country=""
    
    # Try multiple geo APIs
    local geo_apis=(
        "http://ip-api.com/json?fields=countryCode"
        "https://ipapi.co/country_code/"
        "https://ipinfo.io/country"
    )
    
    for api in "${geo_apis[@]}"; do
        local response
        response=$(curl -fsSL --connect-timeout 5 --max-time 10 "$api" 2>/dev/null)
        
        if [ -n "$response" ]; then
            # ip-api.com returns JSON
            if echo "$response" | grep -q "countryCode"; then
                country=$(echo "$response" | grep -o '"countryCode":"[^"]*"' | cut -d'"' -f4)
            else
                # Other APIs return plain text
                country=$(echo "$response" | tr -d '[:space:]' | head -c 2)
            fi
            
            if [ -n "$country" ] && [ ${#country} -eq 2 ]; then
                break
            fi
        fi
    done
    
    if [ -n "$country" ]; then
        SERVER_COUNTRY="$country"
        log_success "$(msg country_detected): $country"
        
        if [ "$country" = "RU" ]; then
            log_info "$(msg country_russia)"
        else
            log_info "$(msg country_other)"
        fi
    else
        SERVER_COUNTRY=""
        log_warn "$(msg country_detection_failed)"
    fi
}

# Build raw file URL based on mirror type
# Usage: build_raw_url "type:base_url" "user/repo" "branch" "file_path"
build_raw_url() {
    local mirror="$1"
    local repo="$2"
    local branch="$3"
    local file_path="$4"
    
    local mirror_type="${mirror%%:*}"
    local mirror_base="${mirror#*:}"
    
    case "$mirror_type" in
        direct)
            echo "https://raw.githubusercontent.com/${repo}/${branch}/${file_path}"
            ;;
        replace)
            # Replace github.com with mirror domain, use raw path
            echo "${mirror_base}/${repo}/raw/${branch}/${file_path}"
            ;;
        proxy)
            # Proxy: prepend proxy URL to full raw URL
            echo "${mirror_base}/https://raw.githubusercontent.com/${repo}/${branch}/${file_path}"
            ;;
    esac
}

# Build git clone URL based on mirror type
# Usage: build_clone_url "type:base_url" "user/repo"
build_clone_url() {
    local mirror="$1"
    local repo="$2"
    
    local mirror_type="${mirror%%:*}"
    local mirror_base="${mirror#*:}"
    
    case "$mirror_type" in
        direct)
            echo "https://github.com/${repo}.git"
            ;;
        replace)
            # Replace github.com with mirror domain
            echo "${mirror_base}/${repo}.git"
            ;;
        proxy)
            # Proxy: prepend proxy URL to full github URL
            echo "${mirror_base}/https://github.com/${repo}.git"
            ;;
    esac
}

# Get display name for mirror
get_mirror_name() {
    local mirror="$1"
    local mirror_type="${mirror%%:*}"
    local mirror_base="${mirror#*:}"
    
    if [ "$mirror_type" = "direct" ]; then
        echo "GitHub (direct)"
    else
        echo "$mirror_base" | sed 's|https://||'
    fi
}

# Test download speed from a mirror (returns speed in KB/s or 0 if failed)
test_mirror_speed() {
    local mirror="$1"
    local test_url
    
    test_url=$(build_raw_url "$mirror" "Joliz1337/monitoring" "main" "VERSION")
    
    # Download with speed measurement
    local result
    result=$(curl -fsSL --connect-timeout 10 --max-time 15 -w "%{speed_download}" -o /dev/null "$test_url" 2>/dev/null)
    
    if [ $? -eq 0 ] && [ -n "$result" ]; then
        # Convert bytes/sec to KB/s
        echo "$result" | awk '{printf "%.0f", $1/1024}'
    else
        echo "0"
    fi
}

# Select the best GitHub mirror based on speed
select_best_mirror() {
    log_info "$(msg testing_mirrors)"
    
    local mirrors=()
    local best_mirror=""
    local best_speed=0
    
    # Select mirror list based on country
    if [ "$SERVER_COUNTRY" = "RU" ]; then
        mirrors=("${GITHUB_MIRRORS_RU[@]}")
    else
        mirrors=("${GITHUB_MIRRORS_OTHER[@]}")
    fi
    
    # Test each mirror
    for mirror in "${mirrors[@]}"; do
        local display_name
        display_name=$(get_mirror_name "$mirror")
        
        echo -n "  Testing $display_name... "
        
        local speed
        speed=$(test_mirror_speed "$mirror")
        
        if [ "$speed" -gt 0 ]; then
            echo -e "${GREEN}${speed} KB/s${NC}"
            
            if [ "$speed" -gt "$best_speed" ]; then
                best_speed="$speed"
                best_mirror="$mirror"
            fi
        else
            echo -e "${RED}$(msg mirror_failed)${NC}"
        fi
    done
    
    # If all mirrors failed, use direct GitHub as fallback
    if [ -z "$best_mirror" ]; then
        log_warn "$(msg all_mirrors_failed)"
        best_mirror="direct:https://github.com"
    else
        local display_name
        display_name=$(get_mirror_name "$best_mirror")
        log_success "$(msg mirror_selected): $display_name (${best_speed} KB/s)"
    fi
    
    ACTIVE_GITHUB_MIRROR="$best_mirror"
}

# Clone repository using selected mirror with fallback
clone_repo_with_mirror() {
    local target_dir="$1"
    local branch="${2:-main}"
    
    # Ensure we have a mirror selected
    if [ -z "$ACTIVE_GITHUB_MIRROR" ]; then
        select_best_mirror
    fi
    
    local mirrors=()
    
    # Build mirror list: active first, then others as fallback
    if [ "$SERVER_COUNTRY" = "RU" ]; then
        mirrors=("$ACTIVE_GITHUB_MIRROR" "${GITHUB_MIRRORS_RU[@]}")
    else
        mirrors=("$ACTIVE_GITHUB_MIRROR" "${GITHUB_MIRRORS_OTHER[@]}")
    fi
    
    # Remove duplicates while preserving order
    local unique_mirrors=()
    local seen=""
    for m in "${mirrors[@]}"; do
        if [[ ! " $seen " =~ " $m " ]]; then
            unique_mirrors+=("$m")
            seen="$seen $m"
        fi
    done
    
    local attempt=0
    for mirror in "${unique_mirrors[@]}"; do
        local repo_url
        repo_url=$(build_clone_url "$mirror" "Joliz1337/monitoring")
        
        local display_name
        display_name=$(get_mirror_name "$mirror")
        
        log_info "Downloading from $display_name..."
        
        rm -rf "$target_dir"
        
        # Try to clone with timeout
        if timeout 180 git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir" 2>&1; then
            log_success "$(msg repo_downloaded)"
            return 0
        fi
        
        attempt=$((attempt + 1))
        
        if [ $attempt -lt ${#unique_mirrors[@]} ]; then
            log_warn "$(msg download_slow)"
        fi
    done
    
    log_error "Failed to download repository from all mirrors"
    return 1
}

# ==================== Network Fix Functions ====================

# Check if Docker Hub is accessible
check_docker_hub() {
    log_info "$(msg checking_docker_network)"
    
    # Try to reach Docker Hub auth endpoint
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        log_success "$(msg docker_network_ok)"
        return 0
    fi
    
    # Try alternative check - ping registry
    if curl -fsSL --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
        log_success "$(msg docker_network_ok)"
        return 0
    fi
    
    log_warn "$(msg docker_network_error)"
    return 1
}

# Check general internet connectivity
check_connectivity() {
    log_info "$(msg checking_connectivity)"
    
    local test_urls=(
        "https://1.1.1.1"
        "https://8.8.8.8"
        "https://google.com"
    )
    
    for url in "${test_urls[@]}"; do
        if curl -fsSL --connect-timeout 5 --max-time 10 "$url" >/dev/null 2>&1; then
            log_success "$(msg connectivity_ok)"
            return 0
        fi
    done
    
    log_error "$(msg connectivity_failed)"
    return 1
}

# Disable IPv6
disable_ipv6() {
    log_info "$(msg disabling_ipv6)"
    
    # sysctl settings
    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    
    # Apply immediately
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf 2>/dev/null || true
    
    # Also apply directly in case sysctl.d is not read
    sysctl -w net.ipv6.conf.all.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.default.disable_ipv6=1 2>/dev/null || true
    sysctl -w net.ipv6.conf.lo.disable_ipv6=1 2>/dev/null || true
    
    log_success "$(msg ipv6_disabled)"
}

# Configure DNS to use Cloudflare and Google
configure_dns() {
    log_info "$(msg configuring_dns)"
    
    # Backup existing resolv.conf
    if [ -f /etc/resolv.conf ] && [ ! -f /etc/resolv.conf.backup ]; then
        cp /etc/resolv.conf /etc/resolv.conf.backup
    fi
    
    # Check if resolv.conf is managed by systemd-resolved
    if [ -L /etc/resolv.conf ] && readlink /etc/resolv.conf | grep -q systemd; then
        # Configure systemd-resolved
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/dns.conf << 'EOF'
[Resolve]
DNS=1.1.1.1 8.8.8.8 1.0.0.1 8.8.4.4
FallbackDNS=9.9.9.9 149.112.112.112
EOF
        systemctl restart systemd-resolved 2>/dev/null || true
    else
        # Direct modification of resolv.conf
        # Remove immutable attribute if set
        chattr -i /etc/resolv.conf 2>/dev/null || true
        
        cat > /etc/resolv.conf << 'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 1.0.0.1
nameserver 8.8.4.4
EOF
    fi
    
    log_success "$(msg dns_configured)"
}

# Configure Docker registry mirrors
configure_docker_mirrors() {
    log_info "$(msg configuring_mirrors)"
    
    local docker_config_dir="/etc/docker"
    local daemon_json="$docker_config_dir/daemon.json"
    
    mkdir -p "$docker_config_dir"
    
    # Create or update daemon.json
    if [ -f "$daemon_json" ]; then
        # Backup existing config
        cp "$daemon_json" "${daemon_json}.backup.$(date +%Y%m%d_%H%M%S)"
        
        # Try to merge with existing config using jq if available
        if command -v jq &>/dev/null; then
            local mirrors_json='["https://mirror.gcr.io","https://registry.docker-cn.com","https://docker.mirrors.ustc.edu.cn"]'
            jq --argjson mirrors "$mirrors_json" '. + {"registry-mirrors": $mirrors}' "$daemon_json" > "${daemon_json}.tmp" && \
                mv "${daemon_json}.tmp" "$daemon_json"
        else
            # Simple replacement if jq not available
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
        # Create new daemon.json
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
    
    log_success "$(msg mirrors_configured)"
}

# Restart Docker service
restart_docker() {
    log_info "$(msg restarting_docker)"
    
    systemctl daemon-reload 2>/dev/null || true
    systemctl restart docker 2>/dev/null || service docker restart 2>/dev/null || true
    
    # Wait for Docker to be ready
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if docker info >/dev/null 2>&1; then
            log_success "$(msg docker_restarted)"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done
    
    log_warn "Docker may need manual restart"
    return 1
}

# Main network fix function
fix_docker_network() {
    log_info "$(msg fixing_docker_network)"
    
    local fixes_applied=0
    
    # Fix 1: Disable IPv6
    echo ""
    log_info "$(msg applying_fix) 1/3: IPv6"
    disable_ipv6
    fixes_applied=$((fixes_applied + 1))
    
    # Fix 2: Configure DNS
    echo ""
    log_info "$(msg applying_fix) 2/3: DNS"
    configure_dns
    fixes_applied=$((fixes_applied + 1))
    
    # Fix 3: Configure Docker mirrors
    echo ""
    log_info "$(msg applying_fix) 3/3: Docker mirrors"
    configure_docker_mirrors
    fixes_applied=$((fixes_applied + 1))
    
    # Restart Docker to apply changes
    echo ""
    restart_docker
    
    # Wait a bit for network to stabilize
    sleep 3
    
    return 0
}

# Build with retry and network fix
docker_build_with_retry() {
    local build_dir="$1"
    local max_retries=3
    local retry=0
    
    cd "$build_dir"
    
    while [ $retry -lt $max_retries ]; do
        log_info "$(msg checking_docker_network)"
        
        # First check if Docker Hub is accessible
        if ! check_docker_hub; then
            log_warn "$(msg docker_network_error)"
            
            if [ $retry -eq 0 ]; then
                echo ""
                log_info "$(msg fixing_docker_network)"
                fix_docker_network
                echo ""
            fi
        fi
        
        # Try to build
        log_info "Building containers (attempt $((retry + 1))/$max_retries)..."
        
        if docker compose build --no-cache 2>&1; then
            log_success "$(msg build_success)"
            return 0
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            log_warn "$(msg build_failed)"
            log_info "$(msg retrying_build)"
            echo ""
            
            # Apply additional fixes on subsequent retries
            if [ $retry -eq 1 ]; then
                fix_docker_network
            fi
            
            sleep 5
        fi
    done
    
    # All retries failed
    log_error "$(msg build_failed)"
    log_error "$(msg network_fix_failed)"
    log_info "$(msg manual_fix_hint)"
    echo ""
    echo "Possible solutions:"
    echo "  1. Check if server has internet access"
    echo "  2. Try using a VPN"
    echo "  3. Check firewall settings"
    echo "  4. Try again later (Docker Hub may be temporarily unavailable)"
    echo ""
    return 1
}

# ==================== Core Functions ====================

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "$(msg run_as_root)"
        exit 1
    fi
}

check_git() {
    if ! command -v git &> /dev/null; then
        log_info "$(msg installing_git)"
        apt-get update && apt-get install -y git
    fi
}

clone_repo() {
    log_info "$(msg downloading_repo)"
    clone_repo_with_mirror "$TMP_DIR" "main"
}

cleanup() {
    rm -rf "$TMP_DIR"
}

# Install CLI command
install_cli() {
    cat > "$BIN_PATH" << 'SCRIPT'
#!/bin/bash
# Monitoring System Manager
# Run this command to manage your monitoring installation

SCRIPT_URL="https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"

# Check if we have a local copy
if [ -f "/opt/monitoring-panel/install.sh" ]; then
    exec bash "/opt/monitoring-panel/install.sh" "$@"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    exec bash "/opt/monitoring-node/install.sh" "$@"
else
    # Download and run
    exec bash <(curl -fsSL "$SCRIPT_URL") "$@"
fi
SCRIPT
    chmod +x "$BIN_PATH"
    log_success "$(msg cli_installed)"
}

# Copy install.sh to installation directories
copy_installer() {
    local target="$1"
    if [ -d "$target" ]; then
        cp "$0" "$target/install.sh" 2>/dev/null || \
        cp "$TMP_DIR/install.sh" "$target/install.sh" 2>/dev/null || true
        chmod +x "$target/install.sh" 2>/dev/null || true
    fi
}

# ==================== System Optimizations ====================

apply_system_optimizations() {
    log_info "$(msg optimizing_system)"
    
    # sysctl config for high connections + anti-DDoS
    cat > /etc/sysctl.d/99-vless-tuning.conf << 'EOF'
# System optimization for high connections + anti-DDoS

# BBR
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq

# File Descriptors
fs.file-max = 2097152
fs.nr_open = 2097152

# Buffers
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 131072 67108864
net.ipv4.tcp_wmem = 4096 87380 67108864
net.ipv4.tcp_moderate_rcvbuf = 1

# Queues
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.netdev_max_backlog = 65535
net.core.netdev_budget = 600

# TCP Performance
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1

# TIME-WAIT
net.ipv4.tcp_max_tw_buckets = 2000000
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535

# Orphans
net.ipv4.tcp_max_orphans = 262144
net.ipv4.tcp_orphan_retries = 1

# Keepalive
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_probes = 3
net.ipv4.tcp_keepalive_intvl = 30

# Anti-DDoS
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_syn_retries = 3
net.ipv4.tcp_synack_retries = 3
net.ipv4.tcp_fin_timeout = 20

# IP spoofing protection
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# ICMP protection
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.secure_redirects = 0

# Conntrack
net.netfilter.nf_conntrack_max = 1048576
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 60
net.netfilter.nf_conntrack_tcp_timeout_close_wait = 30
net.netfilter.nf_conntrack_tcp_timeout_fin_wait = 60
net.netfilter.nf_conntrack_tcp_timeout_established = 3600
EOF
    chmod 644 /etc/sysctl.d/99-vless-tuning.conf
    log_success "sysctl config created"
    
    # Remove old config if exists
    rm -f /etc/sysctl.d/99-haproxy.conf 2>/dev/null || true
    
    # Apply sysctl settings
    sysctl -p /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || log_warn "Some sysctl settings may require kernel support"
    log_success "sysctl settings applied"
    
    # File descriptor limits
    cat > /etc/security/limits.d/99-nofile.conf << 'EOF'
# File descriptor limits for high connections
* soft nofile 2097152
* hard nofile 2097152
root soft nofile 2097152
root hard nofile 2097152
EOF
    log_success "limits.conf configured"
    
    # systemd limits
    if [ -d /etc/systemd/system ]; then
        mkdir -p /etc/systemd/system/user-.slice.d
        cat > /etc/systemd/system/user-.slice.d/limits.conf << 'EOF'
[Slice]
DefaultLimitNOFILE=2097152
EOF
        systemctl daemon-reload 2>/dev/null || true
        log_success "systemd limits configured"
    fi
    
    log_success "$(msg optimizations_applied)"
}

check_optimizations_status() {
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        echo "$(msg applied)"
    else
        echo "$(msg not_applied)"
    fi
}

# ==================== Panel Functions ====================

install_panel() {
    log_info "$(msg installing_panel) $PANEL_DIR..."
    
    if [ -d "$PANEL_DIR" ]; then
        log_warn "$(msg panel_already_installed) $PANEL_DIR"
        read -p "$(msg reinstall_confirm) " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "$(msg installation_cancelled)"
            return 1
        fi
        rm -rf "$PANEL_DIR"
    fi
    
    cp -r "$TMP_DIR/panel" "$PANEL_DIR"
    copy_installer "$PANEL_DIR"
    cd "$PANEL_DIR"
    chmod +x deploy.sh update.sh 2>/dev/null || true
    
    # Run deploy with network-aware build
    ./deploy.sh
    
    install_cli
    log_success "$(msg panel_installed)"
    echo ""
    log_info "$(msg run_monitoring)"
}

update_panel() {
    if [ ! -d "$PANEL_DIR" ]; then
        log_warn "$(msg panel_not_found) $PANEL_DIR"
        return 1
    fi
    
    log_info "$(msg updating_panel)"
    cd "$PANEL_DIR"
    
    if [ -f "update.sh" ]; then
        ./update.sh
    else
        # Fallback: manual update
        clone_repo
        cp "$TMP_DIR/panel/update.sh" "$PANEL_DIR/update.sh"
        chmod +x "$PANEL_DIR/update.sh"
        ./update.sh
    fi
    
    copy_installer "$PANEL_DIR"
    log_success "$(msg update_complete)"
}

remove_panel() {
    if [ ! -d "$PANEL_DIR" ]; then
        log_warn "$(msg panel_not_found) $PANEL_DIR"
        return 1
    fi
    
    read -p "$(msg remove_confirm) " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "$(msg removal_cancelled)"
        return 1
    fi
    
    log_info "$(msg stopping_containers)"
    cd "$PANEL_DIR" && docker compose down -v 2>/dev/null || true
    
    log_info "$(msg removing_files)"
    rm -rf "$PANEL_DIR"
    
    # Remove CLI if both panel and node are uninstalled
    if [ ! -d "$NODE_DIR" ] && [ -f "$BIN_PATH" ]; then
        rm -f "$BIN_PATH"
    fi
    
    log_success "$(msg panel_removed)"
}

# ==================== Node Functions ====================

install_node() {
    log_info "$(msg installing_node) $NODE_DIR..."
    
    if [ -d "$NODE_DIR" ]; then
        log_warn "$(msg node_already_installed) $NODE_DIR"
        read -p "$(msg reinstall_confirm) " confirm
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "$(msg installation_cancelled)"
            return 1
        fi
        rm -rf "$NODE_DIR"
    fi
    
    cp -r "$TMP_DIR/node" "$NODE_DIR"
    copy_installer "$NODE_DIR"
    cd "$NODE_DIR"
    chmod +x deploy.sh update.sh generate-ssl.sh 2>/dev/null || true
    
    # Run deploy with network-aware build
    ./deploy.sh
    
    install_cli
    log_success "$(msg node_installed)"
    echo ""
    log_info "$(msg run_monitoring)"
}

update_node() {
    if [ ! -d "$NODE_DIR" ]; then
        log_warn "$(msg node_not_found) $NODE_DIR"
        return 1
    fi
    
    log_info "$(msg updating_node)"
    cd "$NODE_DIR"
    
    if [ -f "update.sh" ]; then
        ./update.sh
    else
        # Fallback: manual update
        clone_repo
        cp "$TMP_DIR/node/update.sh" "$NODE_DIR/update.sh"
        chmod +x "$NODE_DIR/update.sh"
        ./update.sh
    fi
    
    copy_installer "$NODE_DIR"
    log_success "$(msg update_complete)"
}

remove_node() {
    if [ ! -d "$NODE_DIR" ]; then
        log_warn "$(msg node_not_found) $NODE_DIR"
        return 1
    fi
    
    read -p "$(msg remove_confirm) " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "$(msg removal_cancelled)"
        return 1
    fi
    
    log_info "$(msg stopping_containers)"
    cd "$NODE_DIR" && docker compose down -v 2>/dev/null || true
    
    log_info "$(msg removing_files)"
    rm -rf "$NODE_DIR"
    
    # Remove CLI if both panel and node are uninstalled
    if [ ! -d "$PANEL_DIR" ] && [ -f "$BIN_PATH" ]; then
        rm -f "$BIN_PATH"
    fi
    
    log_success "$(msg node_removed)"
}

# ==================== Language Selection ====================

select_language() {
    clear
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║         Language / Язык                    ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} English"
    echo -e "  ${GREEN}2)${NC} Русский"
    echo ""
    
    read -p "Select / Выберите [1-2]: " lang_choice
    
    case $lang_choice in
        1) LANG_CODE="en" ;;
        2) LANG_CODE="ru" ;;
        *) LANG_CODE="en" ;;
    esac
    
    # Save language preference
    mkdir -p /etc/monitoring 2>/dev/null || true
    echo "$LANG_CODE" > /etc/monitoring/language 2>/dev/null || true
}

load_language() {
    if [ -f /etc/monitoring/language ]; then
        LANG_CODE=$(cat /etc/monitoring/language)
    fi
}

# ==================== Menu ====================

show_menu() {
    clear
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       $(msg menu_title)          ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
    echo ""
    
    # Installation options
    echo -e "  ${GREEN}1)${NC} $(msg menu_install_panel)"
    echo -e "  ${GREEN}2)${NC} $(msg menu_install_node)"
    echo ""
    
    # Update options (only if installed)
    if [ -d "$PANEL_DIR" ]; then
        echo -e "  ${BLUE}3)${NC} $(msg menu_update_panel)"
    fi
    if [ -d "$NODE_DIR" ]; then
        echo -e "  ${BLUE}4)${NC} $(msg menu_update_node)"
    fi
    
    # Remove options
    if [ -d "$PANEL_DIR" ] || [ -d "$NODE_DIR" ]; then
        echo ""
    fi
    if [ -d "$PANEL_DIR" ]; then
        echo -e "  ${RED}5)${NC} $(msg menu_remove_panel)"
    fi
    if [ -d "$NODE_DIR" ]; then
        echo -e "  ${RED}6)${NC} $(msg menu_remove_node)"
    fi
    
    echo ""
    echo -e "  ${CYAN}7)${NC} $(msg menu_optimize_system)"
    
    echo ""
    echo -e "  ${YELLOW}0)${NC} $(msg menu_exit)"
    echo ""
    
    # Show current status
    echo -e "${BLUE}$(msg status):${NC}"
    if [ -d "$PANEL_DIR" ]; then
        local panel_version="?"
        [ -f "$PANEL_DIR/VERSION" ] && panel_version=$(cat "$PANEL_DIR/VERSION")
        echo -e "  Panel: ${GREEN}$(msg installed)${NC} v$panel_version ($PANEL_DIR)"
    else
        echo -e "  Panel: ${YELLOW}$(msg not_installed)${NC}"
    fi
    if [ -d "$NODE_DIR" ]; then
        local node_version="?"
        [ -f "$NODE_DIR/VERSION" ] && node_version=$(cat "$NODE_DIR/VERSION")
        echo -e "  Node:  ${GREEN}$(msg installed)${NC} v$node_version ($NODE_DIR)"
    else
        echo -e "  Node:  ${YELLOW}$(msg not_installed)${NC}"
    fi
    
    # Optimizations status
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        echo -e "  $(msg optimizations_status): ${GREEN}$(msg applied)${NC}"
    else
        echo -e "  $(msg optimizations_status): ${YELLOW}$(msg not_applied)${NC}"
    fi
    echo ""
}

# ==================== Main ====================

main() {
    check_root
    load_language
    
    # First run - select language
    if [ ! -f /etc/monitoring/language ]; then
        select_language
    fi
    
    # Detect server country for mirror selection (once per session)
    if [ -z "$SERVER_COUNTRY" ]; then
        detect_country
        select_best_mirror
        echo ""
    fi
    
    while true; do
        show_menu
        read -p "$(msg select_action): " choice
        
        case $choice in
            1)
                check_git
                clone_repo
                install_panel
                cleanup
                read -p "$(msg press_enter)"
                ;;
            2)
                check_git
                clone_repo
                install_node
                cleanup
                read -p "$(msg press_enter)"
                ;;
            3)
                if [ -d "$PANEL_DIR" ]; then
                    check_git
                    update_panel
                    cleanup
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                read -p "$(msg press_enter)"
                ;;
            4)
                if [ -d "$NODE_DIR" ]; then
                    check_git
                    update_node
                    cleanup
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                read -p "$(msg press_enter)"
                ;;
            5)
                if [ -d "$PANEL_DIR" ]; then
                    remove_panel
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                read -p "$(msg press_enter)"
                ;;
            6)
                if [ -d "$NODE_DIR" ]; then
                    remove_node
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                read -p "$(msg press_enter)"
                ;;
            7)
                apply_system_optimizations
                read -p "$(msg press_enter)"
                ;;
            0)
                echo ""
                log_info "$(msg goodbye)"
                cleanup
                exit 0
                ;;
            l|L|lang)
                select_language
                ;;
            *)
                log_error "$(msg invalid_option)"
                sleep 1
                ;;
        esac
    done
}

# Trap cleanup on exit
trap cleanup EXIT

# Run
main "$@"
