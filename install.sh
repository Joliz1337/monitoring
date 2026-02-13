#!/bin/bash
#
# Monitoring System Installer
# 
# Quick install:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
#
# After installation, run: monitoring
#

# ==================== Safety Settings ====================

# Don't exit on error - we handle errors manually
set +e

# Prevent interactive prompts during package installation
# needrestart on Ubuntu 22.04+ shows ncurses dialog that hangs scripts
# and can restart sshd, killing the SSH session
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

# Prevent running multiple instances
LOCKFILE="/tmp/monitoring-installer.lock"
LOCK_FD=200

# Build log file for error reporting
BUILD_LOG="/tmp/docker_build_$$.log"

# ==================== Timeouts Configuration ====================

TIMEOUT_USER_INPUT=300          # 5 min for user input
TIMEOUT_GIT_CLONE=180           # 3 min for git clone
TIMEOUT_APT_UPDATE=120          # 2 min for apt update
TIMEOUT_APT_INSTALL=300         # 5 min for apt install
TIMEOUT_CURL=60                 # 1 min for curl requests
TIMEOUT_DOCKER_COMPOSE_DOWN=120 # 2 min for docker compose down
TIMEOUT_DOCKER_BUILD=1200       # 20 min for docker build
TIMEOUT_SYSTEMCTL=60            # 1 min for systemctl operations
TIMEOUT_CONNECTIVITY_CHECK=15   # 15 sec for connectivity check

# Retry configuration
MAX_RETRIES=3
RETRY_DELAY=5

# ==================== Trap and Cleanup ====================

acquire_lock() {
    eval "exec $LOCK_FD>$LOCKFILE"
    if ! flock -n $LOCK_FD 2>/dev/null; then
        echo -e "\033[0;31m[ERROR] Another instance of the installer is already running\033[0m"
        echo "If you're sure no other instance is running, remove: $LOCKFILE"
        exit 1
    fi
    echo $$ > "$LOCKFILE"
}

release_lock() {
    flock -u $LOCK_FD 2>/dev/null || true
    rm -f "$LOCKFILE" 2>/dev/null || true
}

cleanup() {
    local exit_code=$?
    
    # Disable trap to prevent recursion
    trap - EXIT INT TERM
    
    # Release lock
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
    
    # Cleanup temp files
    rm -f "$BUILD_LOG" 2>/dev/null || true
    rm -rf "$TMP_DIR" 2>/dev/null || true
    
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

# ==================== Paths ====================

REPO_URL="https://github.com/Joliz1337/monitoring.git"
TMP_DIR="/tmp/monitoring-installer-$$"

PANEL_DIR="/opt/monitoring-panel"
NODE_DIR="/opt/monitoring-node"
BIN_PATH="/usr/local/bin/monitoring"

LANG_CODE="en"

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
MSG_EN[testing_mirrors]="Testing GitHub access..."
MSG_EN[mirror_selected]="Selected"
MSG_EN[mirror_failed]="unavailable"
MSG_EN[all_mirrors_failed]="All sources failed, will try direct GitHub anyway"
MSG_EN[download_slow]="Download failed, trying alternative..."
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
MSG_EN[retry_attempt]="Retry attempt"
MSG_EN[timeout_error]="Operation timed out"
MSG_EN[checking_requirements]="Checking system requirements..."
MSG_EN[requirements_ok]="System requirements OK"
MSG_EN[disk_space_low]="Low disk space"
MSG_EN[memory_low]="Low memory"
MSG_EN[input_timeout]="Input timeout, using default"

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
MSG_RU[testing_mirrors]="Проверка доступа к GitHub..."
MSG_RU[mirror_selected]="Выбрано"
MSG_RU[mirror_failed]="недоступно"
MSG_RU[all_mirrors_failed]="Все источники недоступны, пробуем прямой GitHub"
MSG_RU[download_slow]="Загрузка не удалась, пробуем альтернативу..."
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
MSG_RU[retry_attempt]="Попытка повтора"
MSG_RU[timeout_error]="Превышено время ожидания"
MSG_RU[checking_requirements]="Проверка системных требований..."
MSG_RU[requirements_ok]="Системные требования выполнены"
MSG_RU[disk_space_low]="Мало места на диске"
MSG_RU[memory_low]="Мало оперативной памяти"
MSG_RU[input_timeout]="Тайм-аут ввода, используется значение по умолчанию"

msg() {
    local key="$1"
    if [ "$LANG_CODE" = "ru" ]; then
        echo "${MSG_RU[$key]:-${MSG_EN[$key]:-$key}}"
    else
        echo "${MSG_EN[$key]:-$key}"
    fi
}

# ==================== Logging ====================

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ==================== Safe Execution Helpers ====================

# Read with timeout and default value
# Usage: result=$(safe_read "prompt" "default_value" timeout_sec)
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

# Run command with retry logic
# Usage: run_with_retry max_retries delay_sec "description" command args...
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
    echo "$output"
    return $exit_code
}

# Run command with timeout and retry
# Usage: run_timeout_retry timeout_sec max_retries delay_sec "description" command args...
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
            log_warn "$desc - $(msg timeout_error) (${timeout_sec}s, attempt $attempt/$max_retries)"
        else
            log_warn "$desc - failed (exit $exit_code, attempt $attempt/$max_retries)"
        fi
        
        if [ $attempt -lt $max_retries ]; then
            sleep "$delay"
        fi
        
        attempt=$((attempt + 1))
    done
    
    log_error "$desc - failed after $max_retries attempts"
    if [ -n "$output" ]; then
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo "$output" | tail -30
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    fi
    return 1
}

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

# Run command quietly with timeout
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
            log_warn "$desc - $(msg timeout_error) (${timeout_sec}s)"
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

# Safe file operation with backup
safe_write_file() {
    local file="$1"
    local content="$2"
    local backup="${file}.bak.$(date +%Y%m%d_%H%M%S)"
    
    # Create backup if file exists
    if [ -f "$file" ]; then
        cp "$file" "$backup" 2>/dev/null || true
    fi
    
    # Ensure directory exists
    mkdir -p "$(dirname "$file")" 2>/dev/null || true
    
    # Write content
    if echo "$content" > "$file" 2>/dev/null; then
        return 0
    else
        # Restore backup on failure
        if [ -f "$backup" ]; then
            mv "$backup" "$file" 2>/dev/null || true
        fi
        return 1
    fi
}

# ==================== System Requirements Check ====================

check_disk_space() {
    local required_mb="${1:-2000}"  # 2GB default
    local available_mb
    
    available_mb=$(df -m /opt 2>/dev/null | awk 'NR==2 {print $4}' || echo "0")
    
    if [ "$available_mb" -lt "$required_mb" ]; then
        log_warn "$(msg disk_space_low): ${available_mb}MB available, ${required_mb}MB required"
        return 1
    fi
    return 0
}

check_memory() {
    local required_mb="${1:-512}"  # 512MB default
    local available_mb
    
    available_mb=$(free -m 2>/dev/null | awk '/^Mem:/ {print $7}' || echo "0")
    
    if [ "$available_mb" -lt "$required_mb" ]; then
        log_warn "$(msg memory_low): ${available_mb}MB available, ${required_mb}MB recommended"
        return 1
    fi
    return 0
}

check_requirements() {
    log_info "$(msg checking_requirements)"
    local warnings=0
    
    check_disk_space 2000 || warnings=$((warnings + 1))
    check_memory 512 || warnings=$((warnings + 1))
    
    if [ $warnings -eq 0 ]; then
        log_success "$(msg requirements_ok)"
    fi
    return 0  # Don't fail, just warn
}

# ==================== Network Functions ====================

# Check if Docker Hub is accessible (quiet)
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

# Check Docker Hub with logs
check_docker_hub() {
    log_info "$(msg checking_docker_network)"
    
    if check_docker_hub_quiet; then
        log_success "$(msg docker_network_ok)"
        return 0
    fi
    
    log_warn "$(msg docker_network_error)"
    return 1
}

# Check general internet connectivity (quiet)
check_connectivity_quiet() {
    local test_urls=(
        "https://1.1.1.1"
        "https://8.8.8.8"
        "https://google.com"
    )
    
    for url in "${test_urls[@]}"; do
        if timeout "$TIMEOUT_CONNECTIVITY_CHECK" curl -fsSL --connect-timeout 5 --max-time 10 "$url" >/dev/null 2>&1; then
            return 0
        fi
    done
    
    return 1
}

# Check connectivity with logs
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
    
    # Check if IPv6 is already disabled
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
    
    log_success "$(msg ipv6_disabled)"
}

# Configure DNS
configure_dns() {
    log_info "$(msg configuring_dns)"
    
    # Backup
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
    
    log_success "$(msg dns_configured)"
}

# Configure Docker DNS
configure_docker_dns() {
    log_info "$(msg configuring_dns)"
    
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
    
    log_success "$(msg dns_configured)"
}

# Restart Docker service
restart_docker() {
    log_info "$(msg restarting_docker)"
    
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl restart docker >/dev/null 2>&1 || \
        timeout "$TIMEOUT_SYSTEMCTL" service docker restart >/dev/null 2>&1 || true
    
    local max_wait=30
    local count=0
    while [ $count -lt $max_wait ]; do
        if timeout 5 docker info >/dev/null 2>&1; then
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
    
    disable_ipv6
    configure_dns
    configure_docker_dns
    restart_docker
    
    sleep 3
    return 0
}

# ==================== Git Clone with Retry ====================

clone_repo_with_fallback() {
    local target_dir="$1"
    local branch="${2:-main}"
    local repo_url="https://github.com/Joliz1337/monitoring.git"
    
    rm -rf "$target_dir" 2>/dev/null || true
    
    log_info "$(msg downloading_repo)..."
    
    if run_timeout_retry "$TIMEOUT_GIT_CLONE" "$MAX_RETRIES" "$RETRY_DELAY" "git clone" \
        git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir"; then
        log_success "$(msg repo_downloaded)"
        return 0
    fi
    
    log_error "Failed to download repository"
    return 1
}

# ==================== Docker Build with Retry ====================

docker_build_with_retry() {
    local build_dir="$1"
    local max_retries=$MAX_RETRIES
    local retry=0
    local build_timeout="$TIMEOUT_DOCKER_BUILD"
    
    cd "$build_dir" || return 1
    
    while [ $retry -lt $max_retries ]; do
        # Pre-check network
        if ! check_docker_hub_quiet; then
            log_warn "$(msg docker_network_error)"
            
            if [ $retry -eq 0 ]; then
                log_info "$(msg fixing_docker_network)"
                fix_docker_network
            fi
        fi
        
        log_info "Building containers ($(msg retry_attempt) $((retry + 1))/$max_retries, timeout: ${build_timeout}s)..."
        
        local build_exit_code
        
        # Run build with timeout
        timeout "$build_timeout" docker compose build --no-cache > "$BUILD_LOG" 2>&1 &
        local build_pid=$!
        
        # Progress display
        local dots=""
        while kill -0 $build_pid 2>/dev/null; do
            dots="${dots}."
            if [ ${#dots} -gt 3 ]; then dots="."; fi
            local current_step
            current_step=$(grep -oE 'Step [0-9]+/[0-9]+|#[0-9]+ \[[0-9]+/[0-9]+\]' "$BUILD_LOG" 2>/dev/null | tail -1 || true)
            if [ -n "$current_step" ]; then
                printf "\r${BLUE}[INFO]${NC} Building${dots} %-30s" "($current_step)"
            else
                printf "\r${BLUE}[INFO]${NC} Building${dots}   "
            fi
            sleep 2
        done
        printf "\r%-60s\r" " "
        
        wait $build_pid 2>/dev/null
        build_exit_code=$?
        
        if [ $build_exit_code -eq 0 ]; then
            rm -f "$BUILD_LOG" 2>/dev/null || true
            clear
            echo ""
            echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
            echo -e "${CYAN}║       $(msg menu_title)          ║${NC}"
            echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
            echo ""
            log_success "$(msg build_success)"
            return 0
        elif [ $build_exit_code -eq 124 ]; then
            log_error "Build $(msg timeout_error) (${build_timeout}s)"
            echo -e "${YELLOW}Build was taking too long. Possible causes:${NC}"
            echo "  - Slow internet connection"
            echo "  - Network issues with Docker Hub"
            echo "  - Server ran out of memory (check: free -h)"
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
            log_warn "$(msg build_failed)"
            log_info "$(msg retrying_build)"
            
            if [ $retry -eq 1 ]; then
                fix_docker_network
            fi
            
            sleep "$RETRY_DELAY"
        fi
    done
    
    log_error "$(msg build_failed)"
    log_error "$(msg network_fix_failed)"
    log_info "$(msg manual_fix_hint)"
    echo ""
    echo "Possible solutions:"
    echo "  1. Check if server has internet access"
    echo "  2. Try using a VPN"
    echo "  3. Check firewall settings"
    echo "  4. Try again later"
    echo "  5. Increase timeout: export TIMEOUT_DOCKER_BUILD=3600"
    echo ""
    return 1
}

# ==================== APT Operations with Retry ====================

apt_update_safe() {
    run_timeout_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get update" \
        apt-get update -qq
}

apt_install_safe() {
    local packages="$*"
    run_timeout_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "apt-get install $packages" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq -o Dpkg::Options::="--force-confold" -o Dpkg::Options::="--force-confdef" $packages
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
        apt_update_safe || log_warn "apt update had issues"
        apt_install_safe git || {
            log_error "Failed to install git"
            return 1
        }
    fi
}

clone_repo() {
    log_info "$(msg downloading_repo)"
    clone_repo_with_fallback "$TMP_DIR" "main"
}

cleanup_temp() {
    rm -rf "$TMP_DIR" 2>/dev/null || true
}

install_cli() {
    local script_content='#!/bin/bash
# Monitoring System Manager

GITHUB_URL="https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"
TIMEOUT=120

if [ -f "/opt/monitoring-panel/install.sh" ]; then
    exec bash "/opt/monitoring-panel/install.sh" "$@"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    exec bash "/opt/monitoring-node/install.sh" "$@"
else
    SCRIPT_CONTENT=$(timeout "$TIMEOUT" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT" "$GITHUB_URL" 2>/dev/null)
    if [ -n "$SCRIPT_CONTENT" ]; then
        exec bash -c "$SCRIPT_CONTENT" -- "$@"
    else
        echo "Failed to download installer from GitHub"
        exit 1
    fi
fi'
    
    if safe_write_file "$BIN_PATH" "$script_content"; then
        chmod +x "$BIN_PATH" 2>/dev/null || true
        log_success "$(msg cli_installed)"
    else
        log_warn "Could not install CLI command"
    fi
}

copy_installer() {
    local target="$1"
    if [ -d "$target" ]; then
        cp "$0" "$target/install.sh" 2>/dev/null || \
        cp "$TMP_DIR/install.sh" "$target/install.sh" 2>/dev/null || true
        chmod +x "$target/install.sh" 2>/dev/null || true
    fi
}

# ==================== System Optimizations ====================

cleanup_conflicting_configs() {
    log_info "Cleaning up conflicting system configs..."
    
    # ---- sysctl.d: remove ALL non-system configs except ours ----
    # System files: 10-* (Ubuntu), 99-sysctl.conf (symlink), 99-cloudimg-* (cloud-init), README.*
    for f in /etc/sysctl.d/*.conf; do
        [ -f "$f" ] || continue
        local bname=$(basename "$f")
        case "$bname" in
            10-*)            continue ;;  # Ubuntu system defaults
            99-sysctl.conf)  continue ;;  # Symlink to /etc/sysctl.conf
            99-cloudimg-*)   continue ;;  # Cloud provider config
            99-vless-tuning.conf) continue ;;  # Our config (will be overwritten)
            *)
                rm -f "$f" 2>/dev/null || true
                log_success "Removed conflicting sysctl config: $bname"
                ;;
        esac
    done
    
    # ---- /etc/sysctl.conf: clean all non-comment active lines ----
    # Remove any uncommented parameter lines (net.*, fs.*, vm.*, kernel.*, precedence)
    if [ -f /etc/sysctl.conf ]; then
        local before_lines=$(grep -cE '^[^#[:space:]]' /etc/sysctl.conf 2>/dev/null || echo 0)
        sed -i '/^net\./d; /^fs\./d; /^vm\./d; /^kernel\./d; /^precedence/d' /etc/sysctl.conf 2>/dev/null || true
        sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' /etc/sysctl.conf 2>/dev/null || true
        local after_lines=$(grep -cE '^[^#[:space:]]' /etc/sysctl.conf 2>/dev/null || echo 0)
        [ "$before_lines" != "$after_lines" ] && log_success "Cleaned $((before_lines - after_lines)) entries from /etc/sysctl.conf"
    fi
    
    # ---- limits.d: remove all non-system configs except ours ----
    for f in /etc/security/limits.d/*.conf; do
        [ -f "$f" ] || continue
        local bname=$(basename "$f")
        [ "$bname" = "99-nofile.conf" ] && continue  # Our config
        rm -f "$f" 2>/dev/null || true
        log_success "Removed conflicting limits config: $bname"
    done
    
    # ---- /etc/security/limits.conf: clean custom lines at the end ----
    if [ -f /etc/security/limits.conf ]; then
        if grep -qE '^\*.*nofile|^root.*nofile' /etc/security/limits.conf 2>/dev/null; then
            sed -i '/^\*.*nofile/d; /^root.*nofile/d; /^\*.*nproc/d; /^root.*nproc/d; /^\*.*memlock/d; /^root.*memlock/d' /etc/security/limits.conf 2>/dev/null || true
            log_success "Cleaned custom entries from /etc/security/limits.conf"
        fi
    fi
    
    # ---- Stop and disable third-party tuning services ----
    local third_party_services="
        3x-ui-tuning
        xray-tuning
        marzban-tuning
        network-optimize
        sysctl-tuning
        tcp-tuning
        tcp-bbr
    "
    for svc in $third_party_services; do
        if systemctl list-unit-files "${svc}.service" &>/dev/null 2>&1 && \
           systemctl is-enabled "${svc}.service" &>/dev/null 2>&1; then
            systemctl stop "${svc}.service" >/dev/null 2>&1 || true
            systemctl disable "${svc}.service" >/dev/null 2>&1 || true
            log_success "Disabled third-party service: ${svc}"
        fi
    done
    
    # ---- Remove third-party tuning scripts from common locations ----
    local tuning_scripts="
        /usr/local/bin/network-tuning.sh
        /usr/local/bin/tcp-tuning.sh
        /usr/local/bin/sysctl-tuning.sh
        /opt/3x-ui/tuning.sh
        /opt/marzban/tuning.sh
    "
    for script in $tuning_scripts; do
        if [ -f "$script" ]; then
            rm -f "$script" 2>/dev/null || true
            log_success "Removed third-party tuning script: $script"
        fi
    done
    
    # ---- Clean crontab entries that apply sysctl ----
    if crontab -l 2>/dev/null | grep -qE 'sysctl|network-tun|tcp-tun'; then
        crontab -l 2>/dev/null | grep -vE 'sysctl|network-tun|tcp-tun' | crontab - 2>/dev/null || true
        log_success "Cleaned sysctl-related crontab entries"
    fi
    
    log_success "Conflicting configs cleanup done"
}

apply_system_optimizations() {
    log_info "$(msg optimizing_system)"
    
    cleanup_conflicting_configs
    
    local CONFIG_SRC=""
    if [ -d "$TMP_DIR/configs" ]; then
        CONFIG_SRC="$TMP_DIR/configs"
    elif [ -d "$(dirname "$0")/configs" ]; then
        CONFIG_SRC="$(dirname "$0")/configs"
    fi
    
    if [ -n "$CONFIG_SRC" ] && [ -f "$CONFIG_SRC/sysctl.conf" ]; then
        log_info "Installing optimization configs..."
        
        cp "$CONFIG_SRC/sysctl.conf" /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
        chmod 644 /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
        log_success "sysctl config installed"
        
        if [ -f "$CONFIG_SRC/limits.conf" ]; then
            cp "$CONFIG_SRC/limits.conf" /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            chmod 644 /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            log_success "limits.conf installed"
        fi
        
        if [ -f "$CONFIG_SRC/systemd-limits.conf" ]; then
            mkdir -p /etc/systemd/system.conf.d 2>/dev/null || true
            cp "$CONFIG_SRC/systemd-limits.conf" /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true
            
            mkdir -p /etc/systemd/system/user-.slice.d 2>/dev/null || true
            sed 's/\[Manager\]/[Slice]/' "$CONFIG_SRC/systemd-limits.conf" > /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            
            timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits installed"
        fi
        
        if [ -f "$CONFIG_SRC/network-tune.sh" ]; then
            mkdir -p /opt/monitoring-node/scripts 2>/dev/null || true
            cp "$CONFIG_SRC/network-tune.sh" /opt/monitoring-node/scripts/network-tune.sh 2>/dev/null || true
            chmod +x /opt/monitoring-node/scripts/network-tune.sh 2>/dev/null || true
            log_success "network-tune.sh installed"
        fi
        
        if [ -f "$CONFIG_SRC/network-tune.service" ]; then
            cp "$CONFIG_SRC/network-tune.service" /etc/systemd/system/network-tune.service 2>/dev/null || true
            chmod 644 /etc/systemd/system/network-tune.service 2>/dev/null || true
            log_success "network-tune.service installed"
        fi
        
        if [ -f "$CONFIG_SRC/VERSION" ]; then
            mkdir -p /opt/monitoring-node/configs 2>/dev/null || true
            cp "$CONFIG_SRC/VERSION" /opt/monitoring-node/configs/VERSION 2>/dev/null || true
            chmod 644 /opt/monitoring-node/configs/VERSION 2>/dev/null || true
            log_success "configs VERSION installed"
        fi
    else
        log_info "Downloading optimization configs..."
        
        local GITHUB_RAW="https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs"
        
        download_config() {
            local filename="$1"
            local dest="$2"
            
            if timeout "$TIMEOUT_CURL" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT_CURL" \
                "$GITHUB_RAW/$filename" -o "$dest" 2>/dev/null; then
                return 0
            fi
            return 1
        }
        
        if download_config "sysctl.conf" "/etc/sysctl.d/99-vless-tuning.conf"; then
            chmod 644 /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
            log_success "sysctl config downloaded"
        else
            log_error "Failed to download sysctl.conf"
            return 1
        fi
        
        if download_config "limits.conf" "/etc/security/limits.d/99-nofile.conf"; then
            chmod 644 /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            log_success "limits.conf downloaded"
        fi
        
        mkdir -p /etc/systemd/system.conf.d 2>/dev/null || true
        if download_config "systemd-limits.conf" "/etc/systemd/system.conf.d/limits.conf"; then
            chmod 644 /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true
            
            mkdir -p /etc/systemd/system/user-.slice.d 2>/dev/null || true
            sed 's/\[Manager\]/[Slice]/' /etc/systemd/system.conf.d/limits.conf > /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            
            timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits downloaded"
        fi
        
        mkdir -p /opt/monitoring-node/scripts 2>/dev/null || true
        if download_config "network-tune.sh" "/opt/monitoring-node/scripts/network-tune.sh"; then
            chmod +x /opt/monitoring-node/scripts/network-tune.sh 2>/dev/null || true
            log_success "network-tune.sh downloaded"
        fi
        
        if download_config "network-tune.service" "/etc/systemd/system/network-tune.service"; then
            chmod 644 /etc/systemd/system/network-tune.service 2>/dev/null || true
            log_success "network-tune.service downloaded"
        fi
        
        mkdir -p /opt/monitoring-node/configs 2>/dev/null || true
        if download_config "VERSION" "/opt/monitoring-node/configs/VERSION"; then
            chmod 644 /opt/monitoring-node/configs/VERSION 2>/dev/null || true
            log_success "configs VERSION downloaded"
        fi
    fi
    
    # Load conntrack module BEFORE sysctl (required for nf_conntrack_* params)
    modprobe nf_conntrack >/dev/null 2>&1 || true
    sleep 0.5
    
    log_info "Applying sysctl settings..."
    if ! sysctl -p /etc/sysctl.d/99-vless-tuning.conf >/dev/null 2>&1; then
        log_warn "Some sysctl settings may require kernel support"
    fi
    log_success "sysctl settings applied"
    
    if [ -f /etc/pam.d/common-session ]; then
        if ! grep -q "pam_limits.so" /etc/pam.d/common-session 2>/dev/null; then
            echo "session required pam_limits.so" >> /etc/pam.d/common-session
        fi
    fi
    
    log_info "Enabling network-tune service..."
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl enable network-tune.service >/dev/null 2>&1 || true
    if ! timeout "$TIMEOUT_SYSTEMCTL" systemctl restart network-tune.service >/dev/null 2>&1; then
        log_warn "Service restart failed, trying direct execution..."
        if /opt/monitoring-node/scripts/network-tune.sh >/dev/null 2>&1; then
            log_success "Network tuning applied (direct execution)"
        else
            log_warn "Could not apply network tuning (may need reboot)"
        fi
    else
        log_success "Network tuning service enabled and applied"
    fi
    
    log_info "Verifying optimizations..."
    local verify_ok=true
    
    if [ "$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)" != "bbr" ]; then
        log_warn "BBR not active (kernel may not support it)"
        verify_ok=false
    fi
    
    local hashsize
    hashsize=$(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo "0")
    if [ "$hashsize" -lt 524288 ] 2>/dev/null; then
        log_warn "Conntrack hashsize is $hashsize (expected >=524288)"
        verify_ok=false
    fi
    
    if [ "$verify_ok" = true ]; then
        log_success "All optimizations verified successfully"
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
    
    check_requirements
    
    if [ -d "$PANEL_DIR" ]; then
        log_warn "$(msg panel_already_installed) $PANEL_DIR"
        local confirm
        confirm=$(safe_read "$(msg reinstall_confirm) " "n" 30)
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "$(msg installation_cancelled)"
            return 1
        fi
        
        # Stop containers with timeout (exit dir first)
        if [ -f "$PANEL_DIR/docker-compose.yml" ]; then
            (cd "$PANEL_DIR" && timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down -v >/dev/null 2>&1) || true
        fi
        rm -rf "$PANEL_DIR"
    fi
    
    cp -r "$TMP_DIR/panel" "$PANEL_DIR" || {
        log_error "Failed to copy panel files"
        return 1
    }
    copy_installer "$PANEL_DIR"
    cd "$PANEL_DIR" || return 1
    chmod +x deploy.sh update.sh >/dev/null 2>&1 || true
    
    ./deploy.sh || {
        log_error "Panel deploy failed"
        return 1
    }
    
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
    cd "$PANEL_DIR" || return 1
    
    if [ -f "update.sh" ]; then
        ./update.sh || {
            log_error "Panel update failed"
            return 1
        }
    else
        clone_repo || return 1
        cp "$TMP_DIR/panel/update.sh" "$PANEL_DIR/update.sh" 2>/dev/null || true
        chmod +x "$PANEL_DIR/update.sh" 2>/dev/null || true
        ./update.sh || {
            log_error "Panel update failed"
            return 1
        }
    fi
    
    copy_installer "$PANEL_DIR"
    log_success "$(msg update_complete)"
}

remove_panel() {
    if [ ! -d "$PANEL_DIR" ]; then
        log_warn "$(msg panel_not_found) $PANEL_DIR"
        return 1
    fi
    
    local confirm
    confirm=$(safe_read "$(msg remove_confirm) " "n" 30)
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "$(msg removal_cancelled)"
        return 1
    fi
    
    log_info "$(msg stopping_containers)"
    if [ -f "$PANEL_DIR/docker-compose.yml" ]; then
        (cd "$PANEL_DIR" && timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down -v >/dev/null 2>&1) || true
    fi
    
    log_info "$(msg removing_files)"
    rm -rf "$PANEL_DIR"
    
    if [ ! -d "$NODE_DIR" ] && [ -f "$BIN_PATH" ]; then
        rm -f "$BIN_PATH"
    fi
    
    log_success "$(msg panel_removed)"
}

# ==================== Node Functions ====================

install_node() {
    log_info "$(msg installing_node) $NODE_DIR..."
    
    check_requirements
    
    if [ -d "$NODE_DIR" ]; then
        log_warn "$(msg node_already_installed) $NODE_DIR"
        local confirm
        confirm=$(safe_read "$(msg reinstall_confirm) " "n" 30)
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "$(msg installation_cancelled)"
            return 1
        fi
        
        # Stop containers with timeout (in subshell to not change cwd)
        if [ -f "$NODE_DIR/docker-compose.yml" ]; then
            (cd "$NODE_DIR" && timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down -v >/dev/null 2>&1) || true
        fi
        rm -rf "$NODE_DIR"
    fi
    
    # Install HAProxy
    if ! command -v haproxy &>/dev/null; then
        log_info "Installing HAProxy..."
        apt_update_safe || log_warn "apt update had issues"
        apt_install_safe haproxy || {
            log_error "Failed to install HAProxy"
            return 1
        }
        timeout "$TIMEOUT_SYSTEMCTL" systemctl enable haproxy >/dev/null 2>&1 || true
        log_success "HAProxy installed"
    else
        log_success "HAProxy already installed"
    fi
    
    # Install ipset
    if ! command -v ipset &>/dev/null; then
        log_info "Installing ipset..."
        apt_install_safe ipset || log_warn "ipset installation had issues"
        log_success "ipset installed"
    else
        log_success "ipset already installed"
    fi
    
    mkdir -p /etc/haproxy 2>/dev/null || true
    
    if timeout 5 systemctl is-active --quiet haproxy 2>/dev/null; then
        log_info "HAProxy is already running"
    else
        log_info "HAProxy is not running (will start when rules are configured)"
    fi
    
    cp -r "$TMP_DIR/node" "$NODE_DIR" || {
        log_error "Failed to copy node files"
        return 1
    }
    copy_installer "$NODE_DIR"
    cd "$NODE_DIR" || return 1
    chmod +x deploy.sh update.sh generate-ssl.sh >/dev/null 2>&1 || true
    
    ./deploy.sh || {
        log_error "Node deploy failed"
        return 1
    }
    
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
    cd "$NODE_DIR" || return 1
    
    if [ -f "update.sh" ]; then
        ./update.sh || {
            log_error "Node update failed"
            return 1
        }
    else
        clone_repo || return 1
        cp "$TMP_DIR/node/update.sh" "$NODE_DIR/update.sh" 2>/dev/null || true
        chmod +x "$NODE_DIR/update.sh" 2>/dev/null || true
        ./update.sh || {
            log_error "Node update failed"
            return 1
        }
    fi
    
    copy_installer "$NODE_DIR"
    log_success "$(msg update_complete)"
}

remove_node() {
    if [ ! -d "$NODE_DIR" ]; then
        log_warn "$(msg node_not_found) $NODE_DIR"
        return 1
    fi
    
    local confirm
    confirm=$(safe_read "$(msg remove_confirm) " "n" 30)
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        log_info "$(msg removal_cancelled)"
        return 1
    fi
    
    log_info "$(msg stopping_containers)"
    # Run docker compose down in subshell to not change cwd
    if [ -f "$NODE_DIR/docker-compose.yml" ]; then
        (cd "$NODE_DIR" && timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down -v >/dev/null 2>&1) || true
    fi
    
    log_info "$(msg removing_files)"
    rm -rf "$NODE_DIR"
    
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
    
    local lang_choice
    lang_choice=$(safe_read "Select / Выберите [1-2]: " "1" 30)
    
    case $lang_choice in
        1) LANG_CODE="en" ;;
        2) LANG_CODE="ru" ;;
        *) LANG_CODE="en" ;;
    esac
    
    mkdir -p /etc/monitoring 2>/dev/null || true
    echo "$LANG_CODE" > /etc/monitoring/language 2>/dev/null || true
}

load_language() {
    if [ -f /etc/monitoring/language ]; then
        LANG_CODE=$(cat /etc/monitoring/language 2>/dev/null || echo "en")
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
    
    echo -e "  ${GREEN}1)${NC} $(msg menu_install_panel)"
    echo -e "  ${GREEN}2)${NC} $(msg menu_install_node)"
    echo ""
    
    if [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ]; then
        echo -e "  ${BLUE}3)${NC} $(msg menu_update_panel)"
    fi
    if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ]; then
        echo -e "  ${BLUE}4)${NC} $(msg menu_update_node)"
    fi
    
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
    
    echo -e "${BLUE}$(msg status):${NC}"
    if [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ]; then
        local panel_version="?"
        [ -f "$PANEL_DIR/VERSION" ] && panel_version=$(cat "$PANEL_DIR/VERSION" 2>/dev/null || echo "?")
        echo -e "  Panel: ${GREEN}$(msg installed)${NC} v$panel_version ($PANEL_DIR)"
    elif [ -d "$PANEL_DIR" ]; then
        echo -e "  Panel: ${YELLOW}incomplete${NC} ($PANEL_DIR exists but not configured)"
    else
        echo -e "  Panel: ${YELLOW}$(msg not_installed)${NC}"
    fi
    if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ]; then
        local node_version="?"
        [ -f "$NODE_DIR/VERSION" ] && node_version=$(cat "$NODE_DIR/VERSION" 2>/dev/null || echo "?")
        echo -e "  Node:  ${GREEN}$(msg installed)${NC} v$node_version ($NODE_DIR)"
    elif [ -d "$NODE_DIR" ]; then
        echo -e "  Node:  ${YELLOW}incomplete${NC} ($NODE_DIR exists but not configured)"
    else
        echo -e "  Node:  ${YELLOW}$(msg not_installed)${NC}"
    fi
    
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        echo -e "  $(msg optimizations_status): ${GREEN}$(msg applied)${NC}"
    else
        echo -e "  $(msg optimizations_status): ${YELLOW}$(msg not_applied)${NC}"
    fi
    
    if timeout 5 systemctl is-enabled network-tune.service &>/dev/null 2>&1; then
        echo -e "  RPS/RFS: ${GREEN}$(msg applied)${NC}"
    else
        echo -e "  RPS/RFS: ${YELLOW}$(msg not_applied)${NC}"
    fi
    echo ""
}

# ==================== Main ====================

main() {
    # Acquire lock to prevent parallel execution
    acquire_lock
    
    check_root
    load_language
    
    # First run - select language
    if [ ! -f /etc/monitoring/language ]; then
        select_language
    fi
    
    while true; do
        show_menu
        
        local choice
        choice=$(safe_read "$(msg select_action): " "0" 60)
        
        case $choice in
            1)
                check_git || continue
                clone_repo || continue
                install_panel
                cleanup_temp
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            2)
                check_git || continue
                clone_repo || continue
                install_node
                cleanup_temp
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            3)
                if [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ]; then
                    check_git || continue
                    update_panel
                    cleanup_temp
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            4)
                if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ]; then
                    check_git || continue
                    update_node
                    cleanup_temp
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            5)
                if [ -d "$PANEL_DIR" ]; then
                    remove_panel
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            6)
                if [ -d "$NODE_DIR" ]; then
                    remove_node
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            7)
                apply_system_optimizations
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            0)
                echo ""
                log_info "$(msg goodbye)"
                cleanup_temp
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

# Run
main "$@"
