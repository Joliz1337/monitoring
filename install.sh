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

# Build log file for error reporting
BUILD_LOG="/tmp/docker_build_$$.log"

# Cleanup proxy on exit (inline, doesn't depend on functions)
cleanup_proxy_on_exit() {
    # Remove temporary APT proxy config
    rm -f /etc/apt/apt.conf.d/99proxy-temp 2>/dev/null || true
    # Remove Docker proxy config
    rm -f /etc/systemd/system/docker.service.d/proxy.conf 2>/dev/null || true
    rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true
    # Unset git proxy
    git config --global --unset http.proxy 2>/dev/null || true
    git config --global --unset https.proxy 2>/dev/null || true
    # Reload docker daemon config
    systemctl daemon-reload 2>/dev/null || true
}

# Trap для обработки прерываний
cleanup() {
    local exit_code=$?
    
    # Always cleanup proxy settings on exit
    cleanup_proxy_on_exit
    
    if [ $exit_code -ne 0 ]; then
        echo ""
        echo -e "\033[0;31m[ERROR] Script interrupted or failed (exit code: $exit_code)\033[0m"
        if [ -f "$BUILD_LOG" ] && [ -s "$BUILD_LOG" ]; then
            echo -e "\033[0;31m[ERROR] Last 50 lines of build output:\033[0m"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
            tail -50 "$BUILD_LOG"
            echo -e "\033[0;31m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
        fi
        rm -f "$BUILD_LOG"
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

# Default timeouts (in seconds)
DOCKER_BUILD_TIMEOUT=1800  # 30 minutes

# Proxy settings (empty = no proxy)
HTTP_PROXY_URL=""

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

# GitHub mirror messages - English
MSG_EN[testing_mirrors]="Testing GitHub access..."
MSG_EN[mirror_selected]="Selected"
MSG_EN[mirror_failed]="unavailable"
MSG_EN[all_mirrors_failed]="All sources failed, will try direct GitHub anyway"
MSG_EN[download_slow]="Download failed, trying alternative..."

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

# Proxy messages - English
MSG_EN[proxy_prompt]="Use HTTP proxy? (Enter to skip, or enter proxy)"
MSG_EN[proxy_format_hint]="Formats: ip:port | ip:port:user:pass | http://user:pass@ip:port"
MSG_EN[proxy_localhost_hint]="Example: 192.168.1.1:3128 or http://user:pass@proxy.local:8080"
MSG_EN[proxy_configured]="Proxy configured for all network operations"
MSG_EN[proxy_not_used]="No proxy"
MSG_EN[proxy_invalid]="Invalid proxy format"
MSG_EN[proxy_enabled]="Proxy enabled"

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

# GitHub mirror messages - Russian
MSG_RU[testing_mirrors]="Проверка доступа к GitHub..."
MSG_RU[mirror_selected]="Выбрано"
MSG_RU[mirror_failed]="недоступно"
MSG_RU[all_mirrors_failed]="Все источники недоступны, пробуем прямой GitHub"
MSG_RU[download_slow]="Загрузка не удалась, пробуем альтернативу..."

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

# Proxy messages - Russian
MSG_RU[proxy_prompt]="Использовать HTTP прокси? (Enter - пропустить, или введите прокси)"
MSG_RU[proxy_format_hint]="Форматы: ip:port | ip:port:user:pass | http://user:pass@ip:port"
MSG_RU[proxy_localhost_hint]="Пример: 192.168.1.1:3128 или http://user:pass@proxy.local:8080"
MSG_RU[proxy_configured]="Прокси настроен для всех сетевых операций"
MSG_RU[proxy_not_used]="Без прокси"
MSG_RU[proxy_invalid]="Неверный формат прокси"
MSG_RU[proxy_enabled]="Прокси включён"

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

# Run command quietly, show full output only on error
# Usage: run_quiet "description" command arg1 arg2 ...
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

# Run command quietly with timeout, show full output only on error
# Usage: run_quiet_timeout timeout_sec "description" command arg1 arg2 ...
run_quiet_timeout() {
    local timeout_sec="$1"
    local desc="$2"
    shift 2
    local output
    local exit_code
    
    output=$(timeout "$timeout_sec" "$@" 2>&1)
    exit_code=$?
    
    if [ $exit_code -ne 0 ]; then
        if [ $exit_code -eq 124 ]; then
            log_warn "$desc - timeout (${timeout_sec}s)"
        else
            echo ""
            log_error "$desc - failed (exit code: $exit_code)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo "$output"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo ""
        fi
        return $exit_code
    fi
    
    return 0
}

# ==================== Proxy Functions ====================

# Parse proxy input and normalize to http:// format
# Input formats:
#   ip:port                      -> http://ip:port
#   ip:port:user:pass            -> http://user:pass@ip:port
#   http://user:pass@ip:port     -> http://user:pass@ip:port (unchanged)
#   http://ip:port               -> http://ip:port (unchanged)
parse_proxy_input() {
    local input="$1"
    
    # Empty input = no proxy
    if [ -z "$input" ]; then
        echo ""
        return 0
    fi
    
    # Already has http:// prefix - keep as is
    if [[ "$input" == http://* ]]; then
        echo "$input"
        return 0
    fi
    
    # Has https:// prefix - keep as is
    if [[ "$input" == https://* ]]; then
        echo "$input"
        return 0
    fi
    
    # Format: ip:port:user:pass (4 parts separated by colons)
    # Need to handle IPv4 addresses
    local colon_count=$(echo "$input" | tr -cd ':' | wc -c)
    
    if [ "$colon_count" -eq 3 ]; then
        # ip:port:user:pass format
        local ip=$(echo "$input" | cut -d: -f1)
        local port=$(echo "$input" | cut -d: -f2)
        local user=$(echo "$input" | cut -d: -f3)
        local pass=$(echo "$input" | cut -d: -f4)
        
        if [[ "$port" =~ ^[0-9]+$ ]] && [ -n "$user" ] && [ -n "$pass" ]; then
            echo "http://${user}:${pass}@${ip}:${port}"
            return 0
        fi
    fi
    
    # Simple format: ip:port (validate it has exactly one colon for port)
    if [[ "$input" =~ ^[^:]+:[0-9]+$ ]]; then
        echo "http://$input"
        return 0
    fi
    
    # Invalid format
    return 1
}

# Ask user for proxy configuration (called before each install/update)
ask_proxy() {
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║              HTTP Proxy                    ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  $(msg proxy_format_hint)"
    echo -e "  ${YELLOW}$(msg proxy_localhost_hint)${NC}"
    echo ""
    
    read -p "$(msg proxy_prompt): " proxy_input
    
    if [ -z "$proxy_input" ]; then
        HTTP_PROXY_URL=""
        log_info "$(msg proxy_not_used)"
        return 0
    fi
    
    local parsed_proxy
    parsed_proxy=$(parse_proxy_input "$proxy_input")
    
    if [ $? -eq 0 ] && [ -n "$parsed_proxy" ]; then
        HTTP_PROXY_URL="$parsed_proxy"
        log_success "$(msg proxy_configured): $HTTP_PROXY_URL"
        # Enable proxy for all operations
        enable_system_proxy
    else
        log_error "$(msg proxy_invalid): $proxy_input"
        HTTP_PROXY_URL=""
        return 1
    fi
}

# Enable proxy for ALL system operations (apt, pip, docker, wget, curl, git, etc.)
enable_system_proxy() {
    if [ -z "$HTTP_PROXY_URL" ]; then
        return 0
    fi
    
    log_info "$(msg proxy_enabled): $HTTP_PROXY_URL"
    
    # Set environment variables for all tools
    export http_proxy="$HTTP_PROXY_URL"
    export https_proxy="$HTTP_PROXY_URL"
    export HTTP_PROXY="$HTTP_PROXY_URL"
    export HTTPS_PROXY="$HTTP_PROXY_URL"
    export ALL_PROXY="$HTTP_PROXY_URL"
    export all_proxy="$HTTP_PROXY_URL"
    
    # Git proxy config
    git config --global http.proxy "$HTTP_PROXY_URL" 2>/dev/null || true
    git config --global https.proxy "$HTTP_PROXY_URL" 2>/dev/null || true
    
    # APT proxy config (temporary file, will be removed after install)
    mkdir -p /etc/apt/apt.conf.d 2>/dev/null || true
    cat > /etc/apt/apt.conf.d/99proxy-temp << EOF
Acquire::http::Proxy "$HTTP_PROXY_URL";
Acquire::https::Proxy "$HTTP_PROXY_URL";
EOF
    
    # Docker daemon proxy (for pulling images)
    mkdir -p /etc/systemd/system/docker.service.d 2>/dev/null || true
    cat > /etc/systemd/system/docker.service.d/proxy.conf << EOF
[Service]
Environment="HTTP_PROXY=$HTTP_PROXY_URL"
Environment="HTTPS_PROXY=$HTTP_PROXY_URL"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF
    
    # Reload docker if it's running
    if systemctl is-active --quiet docker 2>/dev/null; then
        systemctl daemon-reload 2>/dev/null || true
        systemctl restart docker 2>/dev/null || true
        # Wait for docker to be ready
        local count=0
        while [ $count -lt 30 ]; do
            if docker info >/dev/null 2>&1; then
                break
            fi
            sleep 1
            count=$((count + 1))
        done
    fi
}

# Disable/cleanup proxy settings after install
# Args: $1 = "no_docker_restart" to skip docker restart (used after successful install)
disable_system_proxy() {
    local skip_docker_restart="${1:-}"
    
    # Remove temporary APT proxy config
    rm -f /etc/apt/apt.conf.d/99proxy-temp 2>/dev/null || true
    
    # Remove Docker proxy config
    rm -f /etc/systemd/system/docker.service.d/proxy.conf 2>/dev/null || true
    rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true
    
    # Unset git proxy
    git config --global --unset http.proxy 2>/dev/null || true
    git config --global --unset https.proxy 2>/dev/null || true
    
    # Reload docker to apply changes (skip restart if containers just started)
    if [ "$skip_docker_restart" != "no_docker_restart" ]; then
        if systemctl is-active --quiet docker 2>/dev/null; then
            systemctl daemon-reload 2>/dev/null || true
            # Don't restart docker - just reload daemon config
            # Docker will use new config on next restart
        fi
    else
        # Just reload daemon without restart to preserve running containers
        systemctl daemon-reload 2>/dev/null || true
    fi
    
    # Unset environment variables
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
    
    HTTP_PROXY_URL=""
}

# Get curl proxy arguments (for commands that need explicit proxy arg)
get_curl_proxy_args() {
    if [ -n "$HTTP_PROXY_URL" ]; then
        echo "--proxy $HTTP_PROXY_URL"
    fi
}

# Get git proxy config (for commands that need explicit proxy arg)
get_git_proxy_args() {
    if [ -n "$HTTP_PROXY_URL" ]; then
        echo "-c http.proxy=$HTTP_PROXY_URL -c https.proxy=$HTTP_PROXY_URL"
    fi
}

# ==================== GitHub Clone Functions ====================

# Clone repository from GitHub
clone_repo_with_fallback() {
    local target_dir="$1"
    local branch="${2:-main}"
    local repo_url="https://github.com/Joliz1337/monitoring.git"
    local git_proxy_args=$(get_git_proxy_args)
    
    rm -rf "$target_dir"
    
    log_info "$(msg downloading_repo)..."
    if [ -n "$git_proxy_args" ]; then
        log_info "Using proxy: $HTTP_PROXY_URL"
        if run_quiet_timeout 120 "git clone" git $git_proxy_args clone --depth 1 --branch "$branch" "$repo_url" "$target_dir"; then
            log_success "$(msg repo_downloaded)"
            return 0
        fi
    else
        if run_quiet_timeout 120 "git clone" git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir"; then
            log_success "$(msg repo_downloaded)"
            return 0
        fi
    fi
    
    log_error "Failed to download repository"
    return 1
}

# ==================== Network Fix Functions ====================

# Check if Docker Hub is accessible (with logs)
check_docker_hub() {
    log_info "$(msg checking_docker_network)"
    local curl_proxy_args=$(get_curl_proxy_args)
    
    # Try to reach Docker Hub auth endpoint
    if curl -fsSL $curl_proxy_args --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        log_success "$(msg docker_network_ok)"
        return 0
    fi
    
    # Try alternative check - ping registry
    if curl -fsSL $curl_proxy_args --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
        log_success "$(msg docker_network_ok)"
        return 0
    fi
    
    log_warn "$(msg docker_network_error)"
    return 1
}

# Check if Docker Hub is accessible (quiet, no logs)
check_docker_hub_quiet() {
    local curl_proxy_args=$(get_curl_proxy_args)
    
    # Try to reach Docker Hub auth endpoint
    if curl -fsSL $curl_proxy_args --connect-timeout 10 --max-time 15 \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" \
        >/dev/null 2>&1; then
        return 0
    fi
    
    # Try alternative check - ping registry
    if curl -fsSL $curl_proxy_args --connect-timeout 10 --max-time 15 \
        "https://registry-1.docker.io/v2/" \
        >/dev/null 2>&1; then
        return 0
    fi
    
    return 1
}

# Check general internet connectivity (quiet version for internal use)
check_connectivity_quiet() {
    local curl_proxy_args=$(get_curl_proxy_args)
    local test_urls=(
        "https://1.1.1.1"
        "https://8.8.8.8"
        "https://google.com"
    )
    
    for url in "${test_urls[@]}"; do
        if curl -fsSL $curl_proxy_args --connect-timeout 5 --max-time 10 "$url" >/dev/null 2>&1; then
            return 0
        fi
    done
    
    return 1
}

# Check general internet connectivity (with logs)
check_connectivity() {
    log_info "$(msg checking_connectivity)"
    
    if check_connectivity_quiet; then
        log_success "$(msg connectivity_ok)"
        return 0
    fi
    
    log_error "$(msg connectivity_failed)"
    return 1
}

# Disable IPv6
disable_ipv6() {
    log_info "$(msg disabling_ipv6)"
    
    # Check if IPv6 is already disabled in optimization config
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ] && grep -q "disable_ipv6 = 1" /etc/sysctl.d/99-vless-tuning.conf; then
        log_success "IPv6 already disabled"
        # Just apply the existing settings quietly
        sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
        sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 || true
        return 0
    fi
    
    # sysctl settings (separate file if optimizations not applied)
    cat > /etc/sysctl.d/99-disable-ipv6.conf << 'EOF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
net.ipv6.conf.lo.disable_ipv6 = 1
EOF
    
    # Apply immediately (quietly)
    sysctl -p /etc/sysctl.d/99-disable-ipv6.conf >/dev/null 2>&1 || true
    
    # Also apply directly in case sysctl.d is not read
    sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true
    sysctl -w net.ipv6.conf.default.disable_ipv6=1 >/dev/null 2>&1 || true
    sysctl -w net.ipv6.conf.lo.disable_ipv6=1 >/dev/null 2>&1 || true
    
    log_success "$(msg ipv6_disabled)"
}

# Configure DNS to use Cloudflare and Google
configure_dns() {
    log_info "$(msg configuring_dns)"
    
    # Backup existing resolv.conf
    if [ -f /etc/resolv.conf ] && [ ! -f /etc/resolv.conf.backup ]; then
        cp /etc/resolv.conf /etc/resolv.conf.backup 2>/dev/null || true
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
        systemctl restart systemd-resolved >/dev/null 2>&1 || true
    else
        # Direct modification of resolv.conf
        # Remove immutable attribute if set
        chattr -i /etc/resolv.conf >/dev/null 2>&1 || true
        
        cat > /etc/resolv.conf << 'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 1.0.0.1
nameserver 8.8.4.4
EOF
    fi
    
    log_success "$(msg dns_configured)"
}

# Configure Docker DNS
configure_docker_dns() {
    log_info "$(msg configuring_dns)"
    
    local docker_config_dir="/etc/docker"
    local daemon_json="$docker_config_dir/daemon.json"
    
    mkdir -p "$docker_config_dir"
    
    # Create or update daemon.json with DNS only
    if [ -f "$daemon_json" ]; then
        cp "$daemon_json" "${daemon_json}.backup.$(date +%Y%m%d_%H%M%S)"
        if command -v jq &>/dev/null; then
            jq '. + {"dns": ["1.1.1.1", "8.8.8.8"]}' "$daemon_json" > "${daemon_json}.tmp" && \
                mv "${daemon_json}.tmp" "$daemon_json"
        else
            cat > "$daemon_json" << 'EOF'
{
    "dns": ["1.1.1.1", "8.8.8.8"]
}
EOF
        fi
    else
        cat > "$daemon_json" << 'EOF'
{
    "dns": ["1.1.1.1", "8.8.8.8"]
}
EOF
    fi
    
    log_success "$(msg dns_configured)"
}

# Restart Docker service
restart_docker() {
    log_info "$(msg restarting_docker)"
    
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart docker >/dev/null 2>&1 || service docker restart >/dev/null 2>&1 || true
    
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
    
    # Fix 1: Disable IPv6
    disable_ipv6
    
    # Fix 2: Configure DNS
    configure_dns
    
    # Fix 3: Configure Docker DNS
    configure_docker_dns
    
    # Restart Docker to apply changes
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
    local build_timeout="${DOCKER_BUILD_TIMEOUT:-1800}"  # 30 min default
    
    cd "$build_dir"
    
    while [ $retry -lt $max_retries ]; do
        # First check if Docker Hub is accessible (quietly)
        if ! check_docker_hub_quiet; then
            log_warn "$(msg docker_network_error)"
            
            if [ $retry -eq 0 ]; then
                log_info "$(msg fixing_docker_network)"
                fix_docker_network
            fi
        fi
        
        # Try to build with timeout
        log_info "Building containers (attempt $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        
        local build_exit_code
        
        # Run build in background, capture output to log file
        set +e
        timeout "$build_timeout" docker compose build --no-cache > "$BUILD_LOG" 2>&1 &
        local build_pid=$!
        
        # Show progress while building
        local dots=""
        while kill -0 $build_pid 2>/dev/null; do
            dots="${dots}."
            if [ ${#dots} -gt 3 ]; then dots="."; fi
            local current_step=$(grep -oE 'Step [0-9]+/[0-9]+|#[0-9]+ \[[0-9]+/[0-9]+\]' "$BUILD_LOG" 2>/dev/null | tail -1)
            if [ -n "$current_step" ]; then
                printf "\r${BLUE}[INFO]${NC} Building${dots} %-30s" "($current_step)"
            else
                printf "\r${BLUE}[INFO]${NC} Building${dots}   "
            fi
            sleep 2
        done
        printf "\r%-60s\r" " "
        
        wait $build_pid
        build_exit_code=$?
        set -e
        
        if [ $build_exit_code -eq 0 ]; then
            log_success "$(msg build_success)"
            rm -f "$BUILD_LOG"
            return 0
        elif [ $build_exit_code -eq 124 ]; then
            log_error "Build timeout after ${build_timeout}s"
            echo -e "${YELLOW}Build was taking too long. This usually means:${NC}"
            echo "  - Very slow internet connection"
            echo "  - Network issues with Docker Hub"
            echo "  - Server ran out of memory (check: free -h)"
            echo ""
            echo -e "${YELLOW}Last 50 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        else
            log_error "Build failed (exit code: $build_exit_code)"
            echo ""
            echo -e "${YELLOW}Last 50 lines of build output:${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            tail -50 "$BUILD_LOG" 2>/dev/null || echo "(no log available)"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        fi
        
        retry=$((retry + 1))
        
        if [ $retry -lt $max_retries ]; then
            log_warn "$(msg build_failed)"
            log_info "$(msg retrying_build)"
            
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
    echo "  5. Increase timeout: export DOCKER_BUILD_TIMEOUT=3600"
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
        run_quiet "apt-get update" apt-get update -qq
        run_quiet "apt-get install git" apt-get install -y -qq git
    fi
}

clone_repo() {
    log_info "$(msg downloading_repo)"
    clone_repo_with_fallback "$TMP_DIR" "main"
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

GITHUB_URL="https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"

# Check if we have a local copy
if [ -f "/opt/monitoring-panel/install.sh" ]; then
    exec bash "/opt/monitoring-panel/install.sh" "$@"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    exec bash "/opt/monitoring-node/install.sh" "$@"
else
    # Download and run from GitHub
    SCRIPT_CONTENT=$(curl -fsSL --connect-timeout 30 --max-time 120 "$GITHUB_URL" 2>/dev/null)
    if [ -n "$SCRIPT_CONTENT" ]; then
        exec bash -c "$SCRIPT_CONTENT" -- "$@"
    else
        echo "Failed to download installer from GitHub"
        exit 1
    fi
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
    
    # Remove old separate IPv6 config if exists (now integrated into main config)
    rm -f /etc/sysctl.d/99-disable-ipv6.conf >/dev/null 2>&1 || true
    
    # Remove old config if exists
    rm -f /etc/sysctl.d/99-haproxy.conf >/dev/null 2>&1 || true
    
    # Determine config source directory (from cloned repo or local configs/)
    local CONFIG_SRC=""
    if [ -d "$TMP_DIR/configs" ]; then
        CONFIG_SRC="$TMP_DIR/configs"
    elif [ -d "$(dirname "$0")/configs" ]; then
        CONFIG_SRC="$(dirname "$0")/configs"
    fi
    
    if [ -n "$CONFIG_SRC" ] && [ -f "$CONFIG_SRC/sysctl.conf" ]; then
        # Use configs from repository
        log_info "Installing optimization configs..."
        
        # Copy sysctl config
        cp "$CONFIG_SRC/sysctl.conf" /etc/sysctl.d/99-vless-tuning.conf
        chmod 644 /etc/sysctl.d/99-vless-tuning.conf
        log_success "sysctl config installed"
        
        # Copy limits config
        if [ -f "$CONFIG_SRC/limits.conf" ]; then
            cp "$CONFIG_SRC/limits.conf" /etc/security/limits.d/99-nofile.conf
            chmod 644 /etc/security/limits.d/99-nofile.conf
            log_success "limits.conf installed"
        fi
        
        # Copy systemd limits config
        if [ -f "$CONFIG_SRC/systemd-limits.conf" ]; then
            mkdir -p /etc/systemd/system.conf.d
            cp "$CONFIG_SRC/systemd-limits.conf" /etc/systemd/system.conf.d/limits.conf
            chmod 644 /etc/systemd/system.conf.d/limits.conf
            
            # Create user slice limits (replace [Manager] with [Slice])
            mkdir -p /etc/systemd/system/user-.slice.d
            sed 's/\[Manager\]/[Slice]/' "$CONFIG_SRC/systemd-limits.conf" > /etc/systemd/system/user-.slice.d/limits.conf
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf
            
            systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits installed"
        fi
        
        # Copy network-tune.sh
        if [ -f "$CONFIG_SRC/network-tune.sh" ]; then
            mkdir -p /opt/monitoring-node/scripts
            cp "$CONFIG_SRC/network-tune.sh" /opt/monitoring-node/scripts/network-tune.sh
            chmod +x /opt/monitoring-node/scripts/network-tune.sh
            log_success "network-tune.sh installed"
        fi
        
        # Copy network-tune.service
        if [ -f "$CONFIG_SRC/network-tune.service" ]; then
            cp "$CONFIG_SRC/network-tune.service" /etc/systemd/system/network-tune.service
            chmod 644 /etc/systemd/system/network-tune.service
            log_success "network-tune.service installed"
        fi
        
        # Copy configs VERSION file for version tracking
        if [ -f "$CONFIG_SRC/VERSION" ]; then
            mkdir -p /opt/monitoring-node/configs
            cp "$CONFIG_SRC/VERSION" /opt/monitoring-node/configs/VERSION
            chmod 644 /opt/monitoring-node/configs/VERSION
            log_success "configs VERSION installed"
        fi
    else
        # Fallback: download configs from GitHub
        log_info "Downloading optimization configs..."
        
        local GITHUB_RAW="https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs"
        
        # Helper function: download from GitHub
        download_config() {
            local filename="$1"
            local dest="$2"
            local curl_proxy_args=$(get_curl_proxy_args)
            
            if curl -fsSL $curl_proxy_args --connect-timeout 30 --max-time 120 "$GITHUB_RAW/$filename" -o "$dest" 2>/dev/null; then
                return 0
            fi
            return 1
        }
        
        # Download sysctl config
        if download_config "sysctl.conf" "/etc/sysctl.d/99-vless-tuning.conf"; then
            chmod 644 /etc/sysctl.d/99-vless-tuning.conf
            log_success "sysctl config downloaded"
        else
            log_error "Failed to download sysctl.conf"
            return 1
        fi
        
        # Download limits config
        if download_config "limits.conf" "/etc/security/limits.d/99-nofile.conf"; then
            chmod 644 /etc/security/limits.d/99-nofile.conf
            log_success "limits.conf downloaded"
        fi
        
        # Download systemd limits config
        mkdir -p /etc/systemd/system.conf.d
        if download_config "systemd-limits.conf" "/etc/systemd/system.conf.d/limits.conf"; then
            chmod 644 /etc/systemd/system.conf.d/limits.conf
            
            # Create user slice limits
            mkdir -p /etc/systemd/system/user-.slice.d
            sed 's/\[Manager\]/[Slice]/' /etc/systemd/system.conf.d/limits.conf > /etc/systemd/system/user-.slice.d/limits.conf
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf
            
            systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits downloaded"
        fi
        
        # Download network-tune.sh
        mkdir -p /opt/monitoring-node/scripts
        if download_config "network-tune.sh" "/opt/monitoring-node/scripts/network-tune.sh"; then
            chmod +x /opt/monitoring-node/scripts/network-tune.sh
            log_success "network-tune.sh downloaded"
        fi
        
        # Download network-tune.service
        if download_config "network-tune.service" "/etc/systemd/system/network-tune.service"; then
            chmod 644 /etc/systemd/system/network-tune.service
            log_success "network-tune.service downloaded"
        fi
        
        # Download configs VERSION file for version tracking
        mkdir -p /opt/monitoring-node/configs
        if download_config "VERSION" "/opt/monitoring-node/configs/VERSION"; then
            chmod 644 /opt/monitoring-node/configs/VERSION
            log_success "configs VERSION downloaded"
        fi
    fi
    
    # Apply sysctl settings (quietly, show warning only if needed)
    log_info "Applying sysctl settings..."
    if ! sysctl -p /etc/sysctl.d/99-vless-tuning.conf >/dev/null 2>&1; then
        log_warn "Some sysctl settings may require kernel support"
    fi
    log_success "sysctl settings applied"
    
    # PAM limits (for SSH sessions)
    if [ -f /etc/pam.d/common-session ]; then
        if ! grep -q "pam_limits.so" /etc/pam.d/common-session; then
            echo "session required pam_limits.so" >> /etc/pam.d/common-session
        fi
    fi
    
    # Load conntrack module if not loaded
    modprobe nf_conntrack >/dev/null 2>&1 || true
    
    # Enable and start network-tune service
    log_info "Enabling network-tune service..."
    systemctl daemon-reload >/dev/null 2>&1
    systemctl enable network-tune.service >/dev/null 2>&1 || true
    if ! systemctl start network-tune.service >/dev/null 2>&1; then
        log_warn "Could not start network-tune service (may need reboot)"
    else
        log_success "Network tuning service enabled"
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

check_network_tune_status() {
    if systemctl is-enabled network-tune.service &>/dev/null; then
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
    chmod +x deploy.sh update.sh >/dev/null 2>&1 || true
    
    # Run deploy with network-aware build
    ./deploy.sh
    
    install_cli
    log_success "$(msg panel_installed)"
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
    cd "$PANEL_DIR" && docker compose down -v >/dev/null 2>&1 || true
    
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
    
    # Install HAProxy if not already installed (native systemd service)
    if ! command -v haproxy &>/dev/null; then
        log_info "Installing HAProxy..."
        run_quiet "apt-get update" apt-get update -qq
        # Use DEBIAN_FRONTEND=noninteractive to avoid config prompts during reinstall
        run_quiet "apt-get install haproxy" env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::="--force-confold" haproxy
        systemctl enable haproxy >/dev/null 2>&1
        log_success "HAProxy installed"
    else
        log_success "HAProxy already installed"
    fi
    
    # Create HAProxy config directory if not exists
    mkdir -p /etc/haproxy
    
    # Check if HAProxy is already running - don't interfere
    if systemctl is-active --quiet haproxy; then
        log_info "HAProxy is already running"
    else
        log_info "HAProxy is not running (will start when rules are configured)"
    fi
    
    cp -r "$TMP_DIR/node" "$NODE_DIR"
    copy_installer "$NODE_DIR"
    cd "$NODE_DIR"
    chmod +x deploy.sh update.sh generate-ssl.sh >/dev/null 2>&1 || true
    
    # Run deploy with network-aware build
    ./deploy.sh
    
    install_cli
    log_success "$(msg node_installed)"
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
    cd "$NODE_DIR" && docker compose down -v >/dev/null 2>&1 || true
    
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
    
    # RPS/RFS status
    if systemctl is-enabled network-tune.service &>/dev/null 2>&1; then
        echo -e "  RPS/RFS: ${GREEN}$(msg applied)${NC}"
    else
        echo -e "  RPS/RFS: ${YELLOW}$(msg not_applied)${NC}"
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
    
    while true; do
        show_menu
        read -p "$(msg select_action): " choice
        
        case $choice in
            1)
                ask_proxy
                check_git
                clone_repo
                install_panel
                cleanup
                disable_system_proxy "no_docker_restart"
                read -p "$(msg press_enter)"
                ;;
            2)
                ask_proxy
                check_git
                clone_repo
                install_node
                cleanup
                disable_system_proxy "no_docker_restart"
                read -p "$(msg press_enter)"
                ;;
            3)
                if [ -d "$PANEL_DIR" ]; then
                    ask_proxy
                    check_git
                    update_panel
                    cleanup
                    disable_system_proxy "no_docker_restart"
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                read -p "$(msg press_enter)"
                ;;
            4)
                if [ -d "$NODE_DIR" ]; then
                    ask_proxy
                    check_git
                    update_node
                    cleanup
                    disable_system_proxy "no_docker_restart"
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
