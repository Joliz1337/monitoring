#!/bin/bash
#
# Monitoring System Installer
# 
# Quick install:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh)
#
# After installation, run: mon
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

# ==================== Timeouts Configuration ====================

TIMEOUT_USER_INPUT=300          # 5 min for user input
TIMEOUT_GIT_CLONE=180           # 3 min for git clone
TIMEOUT_APT_UPDATE=120          # 2 min for apt update
TIMEOUT_APT_INSTALL=300         # 5 min for apt install
TIMEOUT_CURL=60                 # 1 min for curl requests
TIMEOUT_DOCKER_COMPOSE_DOWN=120 # 2 min for docker compose down
TIMEOUT_SYSTEMCTL=60            # 1 min for systemctl operations

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
    fi
    
    # Cleanup temp files
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
REMNAWAVE_DIR="/opt/remnawave"
BIN_PATH="/usr/local/bin/mon"

LANG_CODE="en"

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
MSG_EN[cli_installed]="Command 'mon' installed. Run it anytime to manage your installation."
MSG_EN[run_monitoring]="You can now run: mon"
MSG_EN[menu_optimize_system]="System optimizations (BBR, sysctl, limits)"
MSG_EN[optimizing_system]="Applying system optimizations..."
MSG_EN[optimizations_applied]="System optimizations applied!"
MSG_EN[optimizations_status]="Optimizations"
MSG_EN[applied]="applied"
MSG_EN[not_applied]="not applied"
MSG_EN[opt_select_mode]="Select NIC queue mode:"
MSG_EN[opt_mode_multiqueue]="Multi-queue NIC (hardware multiqueue only)"
MSG_EN[opt_mode_hybrid]="Hybrid (hardware multi-queue + RPS on remaining cores) — recommended when CPU cores > HW queues"
MSG_EN[opt_mode_singlequeue]="Single-queue NIC (software RPS only)"
MSG_EN[nic_hw_detect]="Hardware multiqueue detection"
MSG_EN[nic_hw_supported]="supported"
MSG_EN[nic_hw_not_supported]="not supported"
MSG_EN[nic_hw_max_queues]="max queues"
MSG_EN[nic_hw_ethtool_missing]="ethtool not installed, cannot detect"
MSG_EN[opt_mode_back]="Back"
MSG_EN[rps_removed]="RPS configuration removed"
MSG_EN[rps_not_found]="RPS was not configured"
MSG_EN[multiqueue_removed]="Multiqueue configuration removed"
MSG_EN[multiqueue_not_found]="Multiqueue was not configured"
MSG_EN[hybrid_removed]="Hybrid configuration removed"
MSG_EN[hybrid_not_found]="Hybrid was not configured"
MSG_EN[opt_profile_select]="Select optimization profile:"
MSG_EN[opt_profile_vpn]="VPN (high-traffic relay, 50k+ clients)"
MSG_EN[opt_profile_panel]="Universal (panels, bots, monitoring, websites)"
MSG_EN[opt_installing_configs]="Installing optimization configs"
MSG_EN[opt_downloading_configs]="Downloading optimization configs"
MSG_EN[opt_applying_sysctl]="Applying sysctl settings..."
MSG_EN[opt_sysctl_done]="sysctl settings applied"
MSG_EN[opt_verifying]="Verifying optimizations..."
MSG_EN[opt_verified_ok]="All optimizations verified successfully"
MSG_EN[opt_bbr_inactive]="BBR not active (kernel may not support it)"
MSG_EN[opt_cleaning_conflicts]="Cleaning up conflicting system configs..."
MSG_EN[checking_requirements]="Checking system requirements..."
MSG_EN[requirements_ok]="System requirements OK"
MSG_EN[disk_space_low]="Low disk space"
MSG_EN[memory_low]="Low memory"
MSG_EN[input_timeout]="Input timeout, using default"
MSG_EN[menu_configure_proxy]="Configure proxy"
MSG_EN[proxy_status]="Proxy"
MSG_EN[proxy_enabled]="enabled"
MSG_EN[proxy_disabled]="not configured"
MSG_EN[proxy_current]="Current proxy"
MSG_EN[proxy_enter_address]="Proxy address (host:port)"
MSG_EN[proxy_auth_prompt]="Authentication required? (y/N)"
MSG_EN[proxy_enter_user]="Username"
MSG_EN[proxy_enter_pass]="Password"
MSG_EN[proxy_configured]="Proxy configured!"
MSG_EN[proxy_removed]="Proxy disabled"
MSG_EN[proxy_empty_disable]="Empty = disable proxy"
MSG_EN[proxy_testing]="Testing proxy connection..."
MSG_EN[proxy_test_ok]="Proxy connection OK"
MSG_EN[proxy_test_fail]="Proxy connection failed"
MSG_EN[proxy_save_anyway]="Save anyway? (y/N)"
MSG_EN[proxy_not_saved]="Proxy not saved"
MSG_EN[menu_install_remnawave]="Install Remnawave node"
MSG_EN[menu_install_warp]="Install Cloudflare WARP"
MSG_EN[warp_installing]="Installing Cloudflare WARP..."
MSG_EN[warp_installed]="Cloudflare WARP installed!"
MSG_EN[warp_already_installed]="Cloudflare WARP already installed"
MSG_EN[warp_registering]="Registering WARP..."
MSG_EN[warp_connecting]="Connecting WARP..."
MSG_EN[warp_connected]="WARP connected"
MSG_EN[warp_connect_timeout]="WARP connection timeout. Check manually: warp-cli status"
MSG_EN[warp_autostart_configured]="WARP autostart configured"
MSG_EN[warp_proxy_ok]="WARP proxy working"
MSG_EN[warp_proxy_fail]="Could not verify WARP proxy. Check manually"
MSG_EN[warp_reinstall_confirm]="WARP already installed. Reinstall? (y/N)"
MSG_EN[menu_speed_test]="Speed test"
MSG_EN[speedtest_menu_title]="Speed test — choose tool"
MSG_EN[speedtest_opt_ookla]="Ookla Speedtest (snap)"
MSG_EN[speedtest_opt_iperf]="iperf3 (Russian servers)"
MSG_EN[speedtest_back]="Back"
MSG_EN[speedtest_installing_snapd]="Installing snapd..."
MSG_EN[speedtest_installing_speedtest]="Installing Ookla speedtest (snap)..."
MSG_EN[speedtest_installing_iperf_deps]="Installing iperf3 and dependencies..."
MSG_EN[speedtest_snapd_failed]="Failed to install snapd"
MSG_EN[speedtest_snap_failed]="Failed to install speedtest snap"
MSG_EN[speedtest_iperf_deps_failed]="Failed to install iperf3 dependencies"
MSG_EN[speedtest_iperf_count_prompt]="How many servers to test? [1-10]"
MSG_EN[speedtest_iperf_count_clamped]="Requested count exceeds available servers, using"
MSG_EN[speedtest_iperf_running]="Running iperf3 tests..."
MSG_EN[speedtest_ookla_running]="Running Ookla speedtest..."
MSG_EN[speedtest_removing_cli]="Removing conflicting python speedtest-cli package..."
MSG_EN[remnawave_enter_key]="Paste certificate key from panel (end with empty line):"
MSG_EN[remnawave_installing]="Installing Remnawave node..."
MSG_EN[remnawave_installed]="Remnawave node installed successfully!"
MSG_EN[remnawave_already_installed]="Remnawave already installed at"
MSG_EN[remnawave_reinstall_confirm]="Reinstall? This will remove existing data! (y/N)"
MSG_EN[remnawave_downloading_template]="Downloading masking template..."
MSG_EN[remnawave_template_applied]="Masking template applied"
MSG_EN[remnawave_template_error]="Failed to download/apply template"
MSG_EN[remnawave_docker_installing]="Installing Docker..."
MSG_EN[remnawave_docker_error]="Failed to install Docker"
MSG_EN[remnawave_starting]="Starting Remnawave containers..."
MSG_EN[remnawave_started]="Remnawave containers started"
MSG_EN[remnawave_server_ip]="Server IP"

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
MSG_RU[cli_installed]="Команда 'mon' установлена. Используйте её для управления установкой."
MSG_RU[run_monitoring]="Теперь можно запускать: mon"
MSG_RU[menu_optimize_system]="Системные оптимизации (BBR, sysctl, limits)"
MSG_RU[optimizing_system]="Применение системных оптимизаций..."
MSG_RU[optimizations_applied]="Системные оптимизации применены!"
MSG_RU[optimizations_status]="Оптимизации"
MSG_RU[applied]="применены"
MSG_RU[not_applied]="не применены"
MSG_RU[opt_select_mode]="Выберите режим сетевой карты:"
MSG_RU[opt_mode_multiqueue]="Multi-queue NIC (только аппаратный multiqueue)"
MSG_RU[opt_mode_hybrid]="Hybrid (аппаратный multi-queue + RPS на свободные ядра) — рекомендуется, если ядер больше чем HW-очередей"
MSG_RU[opt_mode_singlequeue]="Обычная NIC (только программный RPS)"
MSG_RU[nic_hw_detect]="Определение аппаратного multiqueue"
MSG_RU[nic_hw_supported]="поддерживается"
MSG_RU[nic_hw_not_supported]="не поддерживается"
MSG_RU[nic_hw_max_queues]="макс. очередей"
MSG_RU[nic_hw_ethtool_missing]="ethtool не установлен, определение невозможно"
MSG_RU[opt_mode_back]="Назад"
MSG_RU[rps_removed]="Конфигурация RPS удалена"
MSG_RU[rps_not_found]="RPS не был настроен"
MSG_RU[multiqueue_removed]="Конфигурация multiqueue удалена"
MSG_RU[multiqueue_not_found]="Multiqueue не был настроен"
MSG_RU[hybrid_removed]="Конфигурация hybrid удалена"
MSG_RU[hybrid_not_found]="Hybrid не был настроен"
MSG_RU[opt_profile_select]="Выберите профиль оптимизации:"
MSG_RU[opt_profile_vpn]="VPN (высоконагруженный релей, 50k+ клиентов)"
MSG_RU[opt_profile_panel]="Универсальный (панели, боты, мониторинг, сайты)"
MSG_RU[opt_installing_configs]="Установка конфигов оптимизации"
MSG_RU[opt_downloading_configs]="Загрузка конфигов оптимизации"
MSG_RU[opt_applying_sysctl]="Применение параметров sysctl..."
MSG_RU[opt_sysctl_done]="Параметры sysctl применены"
MSG_RU[opt_verifying]="Проверка оптимизаций..."
MSG_RU[opt_verified_ok]="Все оптимизации успешно проверены"
MSG_RU[opt_bbr_inactive]="BBR не активен (ядро может не поддерживать)"
MSG_RU[opt_cleaning_conflicts]="Очистка конфликтующих системных конфигов..."
MSG_RU[checking_requirements]="Проверка системных требований..."
MSG_RU[requirements_ok]="Системные требования выполнены"
MSG_RU[disk_space_low]="Мало места на диске"
MSG_RU[memory_low]="Мало оперативной памяти"
MSG_RU[input_timeout]="Тайм-аут ввода, используется значение по умолчанию"
MSG_RU[menu_configure_proxy]="Настроить прокси"
MSG_RU[proxy_status]="Прокси"
MSG_RU[proxy_enabled]="настроен"
MSG_RU[proxy_disabled]="не настроен"
MSG_RU[proxy_current]="Текущий прокси"
MSG_RU[proxy_enter_address]="Адрес прокси (host:port)"
MSG_RU[proxy_auth_prompt]="Требуется авторизация? (y/N)"
MSG_RU[proxy_enter_user]="Имя пользователя"
MSG_RU[proxy_enter_pass]="Пароль"
MSG_RU[proxy_configured]="Прокси настроен!"
MSG_RU[proxy_removed]="Прокси отключен"
MSG_RU[proxy_empty_disable]="Пусто = отключить прокси"
MSG_RU[proxy_testing]="Проверка соединения через прокси..."
MSG_RU[proxy_test_ok]="Прокси работает"
MSG_RU[proxy_test_fail]="Прокси не отвечает"
MSG_RU[proxy_save_anyway]="Сохранить всё равно? (y/N)"
MSG_RU[proxy_not_saved]="Прокси не сохранён"
MSG_RU[menu_install_remnawave]="Установить ноду Remnawave"
MSG_RU[menu_install_warp]="Установить Cloudflare WARP"
MSG_RU[warp_installing]="Установка Cloudflare WARP..."
MSG_RU[warp_installed]="Cloudflare WARP установлен!"
MSG_RU[warp_already_installed]="Cloudflare WARP уже установлен"
MSG_RU[warp_registering]="Регистрация WARP..."
MSG_RU[warp_connecting]="Подключение WARP..."
MSG_RU[warp_connected]="WARP подключён"
MSG_RU[warp_connect_timeout]="Таймаут подключения WARP. Проверьте вручную: warp-cli status"
MSG_RU[warp_autostart_configured]="Автозапуск WARP настроен"
MSG_RU[warp_proxy_ok]="WARP прокси работает"
MSG_RU[warp_proxy_fail]="Не удалось проверить WARP прокси. Проверьте вручную"
MSG_RU[warp_reinstall_confirm]="WARP уже установлен. Переустановить? (y/N)"
MSG_RU[menu_speed_test]="Проверка скорости"
MSG_RU[speedtest_menu_title]="Проверка скорости — выбор инструмента"
MSG_RU[speedtest_opt_ookla]="Ookla Speedtest (snap)"
MSG_RU[speedtest_opt_iperf]="iperf3 (российские серверы)"
MSG_RU[speedtest_back]="Назад"
MSG_RU[speedtest_installing_snapd]="Установка snapd..."
MSG_RU[speedtest_installing_speedtest]="Установка Ookla speedtest (snap)..."
MSG_RU[speedtest_installing_iperf_deps]="Установка iperf3 и зависимостей..."
MSG_RU[speedtest_snapd_failed]="Не удалось установить snapd"
MSG_RU[speedtest_snap_failed]="Не удалось установить speedtest через snap"
MSG_RU[speedtest_iperf_deps_failed]="Не удалось установить зависимости iperf3"
MSG_RU[speedtest_iperf_count_prompt]="Сколько серверов проверить? [1-10]"
MSG_RU[speedtest_iperf_count_clamped]="Запрошено больше, чем доступно серверов, используется"
MSG_RU[speedtest_iperf_running]="Запуск тестов iperf3..."
MSG_RU[speedtest_ookla_running]="Запуск Ookla speedtest..."
MSG_RU[speedtest_removing_cli]="Удаление конфликтующего python-пакета speedtest-cli..."
MSG_RU[remnawave_enter_key]="Вставьте ключ-сертификат из панели (завершите пустой строкой):"
MSG_RU[remnawave_installing]="Установка ноды Remnawave..."
MSG_RU[remnawave_installed]="Нода Remnawave успешно установлена!"
MSG_RU[remnawave_already_installed]="Remnawave уже установлена в"
MSG_RU[remnawave_reinstall_confirm]="Переустановить? Все данные будут удалены! (y/N)"
MSG_RU[remnawave_downloading_template]="Загрузка маскировочного шаблона..."
MSG_RU[remnawave_template_applied]="Маскировочный шаблон применён"
MSG_RU[remnawave_template_error]="Не удалось загрузить/применить шаблон"
MSG_RU[remnawave_docker_installing]="Установка Docker..."
MSG_RU[remnawave_docker_error]="Не удалось установить Docker"
MSG_RU[remnawave_starting]="Запуск контейнеров Remnawave..."
MSG_RU[remnawave_started]="Контейнеры Remnawave запущены"
MSG_RU[remnawave_server_ip]="IP сервера"

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

# Run command with animated spinner showing elapsed time
# Usage: spin "Installing HAProxy" apt-get install -y haproxy
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

# Run with spinner + timeout + automatic retry
# Usage: spin_retry 300 3 5 "Installing packages" apt-get install -y pkg
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

# Disable needrestart interactive prompts permanently (Ubuntu 22.04+)
suppress_needrestart() {
    if [ -d /etc/needrestart ] || dpkg -l needrestart &>/dev/null 2>&1; then
        mkdir -p /etc/needrestart/conf.d 2>/dev/null || true
        echo '$nrconf{restart} = "l";' > /etc/needrestart/conf.d/no-prompt.conf 2>/dev/null || true
    fi
    pkill -9 needrestart 2>/dev/null || true
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

# ==================== Git Clone with Retry ====================

clone_repo_with_fallback() {
    local target_dir="$1"
    local branch="${2:-main}"
    local repo_url="https://github.com/Joliz1337/monitoring.git"

    rm -rf "$target_dir" 2>/dev/null || true

    if spin_retry "$TIMEOUT_GIT_CLONE" "$MAX_RETRIES" "$RETRY_DELAY" "$(msg downloading_repo)" \
        git clone --depth 1 --branch "$branch" "$repo_url" "$target_dir"; then
        return 0
    fi

    log_error "Failed to download repository"
    return 1
}

# ==================== APT Operations ====================

wait_for_apt_lock() {
    local max_wait=120
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
        if [ $waited -eq 0 ]; then
            log_warn "Waiting for apt lock..."
        fi
        sleep 3
        waited=$((waited + 3))
        if [ $waited -ge $max_wait ]; then
            log_warn "apt lock wait timeout (${max_wait}s), trying anyway..."
            return 0
        fi
    done
    return 0
}

apt_update_safe() {
    suppress_needrestart
    wait_for_apt_lock
    spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "Updating package lists" \
        env DEBIAN_FRONTEND=noninteractive \
        apt-get update -qq
}

apt_install_safe() {
    local packages="$*"
    suppress_needrestart
    wait_for_apt_lock
    spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "Installing: $packages" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        $packages
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
# Monitoring System Manager — auto-update via GitHub

if [ -f /etc/monitoring/proxy.conf ]; then
    . /etc/monitoring/proxy.conf 2>/dev/null
    if [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ]; then
        export http_proxy="$PROXY_URL" https_proxy="$PROXY_URL"
        export HTTP_PROXY="$PROXY_URL" HTTPS_PROXY="$PROXY_URL"
        export no_proxy="localhost,127.0.0.1,::1"
    fi
fi

GITHUB_URL="https://raw.githubusercontent.com/Joliz1337/monitoring/main/install.sh"
TIMEOUT=120

SCRIPT_CONTENT=$(timeout "$TIMEOUT" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT" "$GITHUB_URL" 2>/dev/null)
if [ -n "$SCRIPT_CONTENT" ]; then
    exec bash -c "$SCRIPT_CONTENT" -- "$@"
elif [ -f "/opt/monitoring-panel/install.sh" ]; then
    exec bash "/opt/monitoring-panel/install.sh" "$@"
elif [ -f "/opt/monitoring-node/install.sh" ]; then
    exec bash "/opt/monitoring-node/install.sh" "$@"
else
    echo "Failed to download installer from GitHub and no local copy found"
    exit 1
fi'
    
    if safe_write_file "$BIN_PATH" "$script_content"; then
        chmod +x "$BIN_PATH" 2>/dev/null || true
        [ -f "/usr/local/bin/monitoring" ] && rm -f "/usr/local/bin/monitoring" 2>/dev/null || true
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
        local before_lines
        before_lines=$(grep -cE '^[^#[:space:]]' /etc/sysctl.conf 2>/dev/null) || before_lines=0
        sed -i '/^net\./d; /^fs\./d; /^vm\./d; /^kernel\./d; /^precedence/d' /etc/sysctl.conf 2>/dev/null || true
        sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' /etc/sysctl.conf 2>/dev/null || true
        local after_lines
        after_lines=$(grep -cE '^[^#[:space:]]' /etc/sysctl.conf 2>/dev/null) || after_lines=0
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

remove_rps() {
    local removed=false

    if timeout 5 systemctl is-enabled network-tune.service &>/dev/null 2>&1; then
        timeout "$TIMEOUT_SYSTEMCTL" systemctl stop network-tune.service >/dev/null 2>&1 || true
        timeout "$TIMEOUT_SYSTEMCTL" systemctl disable network-tune.service >/dev/null 2>&1 || true
        removed=true
    fi

    rm -f /etc/systemd/system/network-tune.service 2>/dev/null || true
    rm -f /opt/monitoring/scripts/network-tune.sh 2>/dev/null || true

    # Reset RPS/RFS on all interfaces
    for queue_dir in /sys/class/net/*/queues; do
        [ -d "$queue_dir" ] || continue
        for rx_dir in "$queue_dir"/rx-*; do
            [ -d "$rx_dir" ] || continue
            echo 0 > "$rx_dir/rps_cpus" 2>/dev/null || true
            echo 0 > "$rx_dir/rps_flow_cnt" 2>/dev/null || true
        done
    done
    echo 0 > /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || true

    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true

    if [ "$removed" = true ]; then
        log_success "$(msg rps_removed)"
    else
        log_info "$(msg rps_not_found)"
    fi
}

remove_multiqueue() {
    local removed=false

    if timeout 5 systemctl is-enabled multiqueue-tune.service &>/dev/null 2>&1; then
        timeout "$TIMEOUT_SYSTEMCTL" systemctl stop multiqueue-tune.service >/dev/null 2>&1 || true
        timeout "$TIMEOUT_SYSTEMCTL" systemctl disable multiqueue-tune.service >/dev/null 2>&1 || true
        removed=true
    fi

    rm -f /etc/systemd/system/multiqueue-tune.service 2>/dev/null || true
    rm -f /opt/monitoring/scripts/multiqueue-tune.sh 2>/dev/null || true

    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true

    if [ "$removed" = true ]; then
        log_success "$(msg multiqueue_removed)"
    else
        log_info "$(msg multiqueue_not_found)"
    fi
}

remove_hybrid() {
    local removed=false

    if timeout 5 systemctl is-enabled hybrid-tune.service &>/dev/null 2>&1; then
        timeout "$TIMEOUT_SYSTEMCTL" systemctl stop hybrid-tune.service >/dev/null 2>&1 || true
        timeout "$TIMEOUT_SYSTEMCTL" systemctl disable hybrid-tune.service >/dev/null 2>&1 || true
        removed=true
    fi

    rm -f /etc/systemd/system/hybrid-tune.service 2>/dev/null || true
    rm -f /opt/monitoring/scripts/hybrid-tune.sh 2>/dev/null || true

    for queue_dir in /sys/class/net/*/queues; do
        [ -d "$queue_dir" ] || continue
        for rx_dir in "$queue_dir"/rx-*; do
            [ -d "$rx_dir" ] || continue
            echo 0 > "$rx_dir/rps_cpus" 2>/dev/null || true
            echo 0 > "$rx_dir/rps_flow_cnt" 2>/dev/null || true
        done
    done
    echo 0 > /proc/sys/net/core/rps_sock_flow_entries 2>/dev/null || true

    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true

    if [ "$removed" = true ]; then
        log_success "$(msg hybrid_removed)"
    else
        log_info "$(msg hybrid_not_found)"
    fi
}

# Installs tune script + service for the selected NIC mode
# $1 = "multiqueue" | "hybrid" | "rps"
install_nic_tune() {
    local nic_mode="$1"
    local OPT_DIR="/opt/monitoring"
    local CONFIG_SRC="$2"

    local script_name service_name
    case "$nic_mode" in
        multiqueue)
            script_name="multiqueue-tune.sh"
            service_name="multiqueue-tune.service"
            ;;
        hybrid)
            script_name="hybrid-tune.sh"
            service_name="hybrid-tune.service"
            ;;
        *)
            script_name="network-tune.sh"
            service_name="network-tune.service"
            ;;
    esac

    if [ -n "$CONFIG_SRC" ]; then
        if [ -f "$CONFIG_SRC/$script_name" ]; then
            mkdir -p "$OPT_DIR/scripts" 2>/dev/null || true
            cp "$CONFIG_SRC/$script_name" "$OPT_DIR/scripts/$script_name" 2>/dev/null || true
            chmod +x "$OPT_DIR/scripts/$script_name" 2>/dev/null || true
            log_success "$script_name installed"
        fi

        if [ -f "$CONFIG_SRC/$service_name" ]; then
            cp "$CONFIG_SRC/$service_name" "/etc/systemd/system/$service_name" 2>/dev/null || true
            chmod 644 "/etc/systemd/system/$service_name" 2>/dev/null || true
            log_success "$service_name installed"
        fi
    else
        local GITHUB_RAW="https://raw.githubusercontent.com/Joliz1337/monitoring/main/configs"

        mkdir -p "$OPT_DIR/scripts" 2>/dev/null || true
        if timeout "$TIMEOUT_CURL" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT_CURL" \
            "$GITHUB_RAW/$script_name" -o "$OPT_DIR/scripts/$script_name" 2>/dev/null; then
            chmod +x "$OPT_DIR/scripts/$script_name" 2>/dev/null || true
            log_success "$script_name downloaded"
        fi

        if timeout "$TIMEOUT_CURL" curl -fsSL --connect-timeout 30 --max-time "$TIMEOUT_CURL" \
            "$GITHUB_RAW/$service_name" -o "/etc/systemd/system/$service_name" 2>/dev/null; then
            chmod 644 "/etc/systemd/system/$service_name" 2>/dev/null || true
            log_success "$service_name downloaded"
        fi
    fi
}

# Enables and starts the tune service, falls back to direct execution
# $1 = service name, $2 = script path
enable_tune_service() {
    local service_name="$1"
    local script_path="$2"

    log_info "Enabling $service_name..."
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl enable "$service_name" >/dev/null 2>&1 || true
    if ! timeout "$TIMEOUT_SYSTEMCTL" systemctl restart "$service_name" >/dev/null 2>&1; then
        log_warn "Service restart failed, trying direct execution..."
        if "$script_path" >/dev/null 2>&1; then
            log_success "Tuning applied (direct execution)"
        else
            log_warn "Could not apply tuning (may need reboot)"
        fi
    else
        log_success "$service_name enabled and applied"
    fi
}

detect_multiqueue_support() {
    if ! command -v ethtool &>/dev/null; then
        echo -e "  ${YELLOW}⚠ $(msg nic_hw_ethtool_missing)${NC}"
        return
    fi

    local found_any=false
    for dev_path in /sys/class/net/*; do
        [ -d "$dev_path" ] || continue
        [ -d "$dev_path/device" ] || continue
        [ -d "$dev_path/bridge" ] && continue
        [ -f "$dev_path/bonding/slaves" ] && continue
        [ "$(cat "$dev_path/operstate" 2>/dev/null)" = "up" ] || continue

        local iface=$(basename "$dev_path")
        local max_combined
        max_combined=$(ethtool -l "$iface" 2>/dev/null | awk '/Pre-set maximums/,/Current/ { if (/Combined:/) print $2 }' | head -1)
        [ -z "$max_combined" ] && continue

        found_any=true
        if [ "$max_combined" -gt 1 ] 2>/dev/null; then
            echo -e "  ${GREEN}✓${NC} $iface: $(msg nic_hw_supported) ($(msg nic_hw_max_queues): $max_combined)"
        else
            echo -e "  ${YELLOW}✗${NC} $iface: $(msg nic_hw_not_supported) ($(msg nic_hw_max_queues): $max_combined)"
        fi
    done

    if ! $found_any; then
        echo -e "  ${YELLOW}✗ $(msg nic_hw_not_supported)${NC}"
    fi
}

apply_system_optimizations() {
    echo ""
    echo -e "  ${CYAN}$(msg opt_profile_select)${NC}"
    echo -e "  ${CYAN}1)${NC} $(msg opt_profile_vpn)"
    echo -e "  ${CYAN}2)${NC} $(msg opt_profile_panel)"
    echo -e "  ${YELLOW}0)${NC} $(msg opt_mode_back)"
    echo ""

    local profile_choice
    profile_choice=$(safe_read "$(msg select_action): " "0" 30)

    local opt_profile
    case "$profile_choice" in
        1) opt_profile="vpn" ;;
        2) opt_profile="panel" ;;
        *) return 0 ;;
    esac

    echo ""
    echo -e "  ${CYAN}$(msg nic_hw_detect):${NC}"
    detect_multiqueue_support
    echo ""
    echo -e "  $(msg opt_select_mode)"
    echo -e "  ${CYAN}1)${NC} $(msg opt_mode_multiqueue)"
    echo -e "  ${CYAN}2)${NC} $(msg opt_mode_hybrid)"
    echo -e "  ${CYAN}3)${NC} $(msg opt_mode_singlequeue)"
    echo -e "  ${YELLOW}0)${NC} $(msg opt_mode_back)"
    echo ""

    local mode
    mode=$(safe_read "$(msg select_action): " "0" 30)

    local nic_mode
    case "$mode" in
        1) nic_mode="multiqueue" ;;
        2) nic_mode="hybrid" ;;
        3) nic_mode="rps" ;;
        *) return 0 ;;
    esac

    log_info "$(msg optimizing_system)"

    cleanup_conflicting_configs

    local CONFIG_SRC=""
    if [ -d "$TMP_DIR/configs" ]; then
        CONFIG_SRC="$TMP_DIR/configs"
    elif [ -n "$0" ] && [ -d "$(dirname "$0")/configs" ]; then
        CONFIG_SRC="$(dirname "$0")/configs"
    fi

    local OPT_DIR="/opt/monitoring"

    # Migrate from old /opt/monitoring-node/ paths if they exist
    if [ -f "/opt/monitoring-node/scripts/network-tune.sh" ] && [ ! -f "$OPT_DIR/scripts/network-tune.sh" ]; then
        mkdir -p "$OPT_DIR/scripts" 2>/dev/null || true
        mv "/opt/monitoring-node/scripts/network-tune.sh" "$OPT_DIR/scripts/network-tune.sh" 2>/dev/null || true
        rmdir "/opt/monitoring-node/scripts" 2>/dev/null || true
    fi
    if [ -f "/opt/monitoring-node/configs/VERSION" ] && [ ! -f "$OPT_DIR/configs/VERSION" ]; then
        mkdir -p "$OPT_DIR/configs" 2>/dev/null || true
        mv "/opt/monitoring-node/configs/VERSION" "$OPT_DIR/configs/VERSION" 2>/dev/null || true
        rmdir "/opt/monitoring-node/configs" 2>/dev/null || true
    fi
    if [ -f "/etc/systemd/system/network-tune.service" ]; then
        if grep -q "/opt/monitoring-node/scripts/" /etc/systemd/system/network-tune.service 2>/dev/null; then
            sed -i 's|/opt/monitoring-node/scripts/|/opt/monitoring/scripts/|g' /etc/systemd/system/network-tune.service 2>/dev/null || true
            timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
        fi
    fi
    if [ -d "/opt/monitoring-node" ] && [ ! -f "/opt/monitoring-node/docker-compose.yml" ]; then
        rmdir "/opt/monitoring-node/scripts" 2>/dev/null || true
        rmdir "/opt/monitoring-node/configs" 2>/dev/null || true
        rmdir "/opt/monitoring-node" 2>/dev/null || true
    fi

    # ---- Install profile-specific configs (sysctl, limits, systemd-limits) ----
    local PROFILE_SRC=""
    if [ -n "$CONFIG_SRC" ] && [ -f "$CONFIG_SRC/$opt_profile/sysctl.conf" ]; then
        PROFILE_SRC="$CONFIG_SRC/$opt_profile"
    fi

    if [ -n "$PROFILE_SRC" ]; then
        log_info "Installing optimization configs (profile: $opt_profile)..."

        cp "$PROFILE_SRC/sysctl.conf" /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
        chmod 644 /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
        log_success "sysctl config installed ($opt_profile)"

        if [ -f "$PROFILE_SRC/limits.conf" ]; then
            cp "$PROFILE_SRC/limits.conf" /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            chmod 644 /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            log_success "limits.conf installed"
        fi

        if [ -f "$PROFILE_SRC/systemd-limits.conf" ]; then
            mkdir -p /etc/systemd/system.conf.d 2>/dev/null || true
            cp "$PROFILE_SRC/systemd-limits.conf" /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true

            mkdir -p /etc/systemd/system/user-.slice.d 2>/dev/null || true
            sed 's/\[Manager\]/[Slice]/' "$PROFILE_SRC/systemd-limits.conf" > /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true

            timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits installed"
        fi

        if [ -f "$CONFIG_SRC/VERSION" ]; then
            mkdir -p "$OPT_DIR/configs" 2>/dev/null || true
            cp "$CONFIG_SRC/VERSION" "$OPT_DIR/configs/VERSION" 2>/dev/null || true
            chmod 644 "$OPT_DIR/configs/VERSION" 2>/dev/null || true
            log_success "configs VERSION installed"
        fi
    else
        log_info "Downloading optimization configs (profile: $opt_profile)..."

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

        if download_config "$opt_profile/sysctl.conf" "/etc/sysctl.d/99-vless-tuning.conf"; then
            chmod 644 /etc/sysctl.d/99-vless-tuning.conf 2>/dev/null || true
            log_success "sysctl config downloaded ($opt_profile)"
        else
            log_error "Failed to download sysctl.conf"
            return 1
        fi

        if download_config "$opt_profile/limits.conf" "/etc/security/limits.d/99-nofile.conf"; then
            chmod 644 /etc/security/limits.d/99-nofile.conf 2>/dev/null || true
            log_success "limits.conf downloaded"
        fi

        mkdir -p /etc/systemd/system.conf.d 2>/dev/null || true
        if download_config "$opt_profile/systemd-limits.conf" "/etc/systemd/system.conf.d/limits.conf"; then
            chmod 644 /etc/systemd/system.conf.d/limits.conf 2>/dev/null || true

            mkdir -p /etc/systemd/system/user-.slice.d 2>/dev/null || true
            sed 's/\[Manager\]/[Slice]/' /etc/systemd/system.conf.d/limits.conf > /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true
            chmod 644 /etc/systemd/system/user-.slice.d/limits.conf 2>/dev/null || true

            timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
            log_success "systemd limits downloaded"
        fi

        mkdir -p "$OPT_DIR/configs" 2>/dev/null || true
        if download_config "VERSION" "$OPT_DIR/configs/VERSION"; then
            chmod 644 "$OPT_DIR/configs/VERSION" 2>/dev/null || true
            log_success "configs VERSION downloaded"
        fi
    fi

    # Save optimization profile marker
    mkdir -p "$OPT_DIR/configs" 2>/dev/null || true
    echo "$opt_profile" > "$OPT_DIR/configs/OPT_PROFILE" 2>/dev/null || true
    chmod 644 "$OPT_DIR/configs/OPT_PROFILE" 2>/dev/null || true
    log_success "Profile marker saved: $opt_profile"

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

    # ---- Remove all NIC tune modes, then install the selected one ----
    remove_hybrid
    remove_multiqueue
    remove_rps

    case "$nic_mode" in
        multiqueue)
            install_nic_tune "multiqueue" "$CONFIG_SRC"
            enable_tune_service "multiqueue-tune.service" "$OPT_DIR/scripts/multiqueue-tune.sh"
            ;;
        hybrid)
            install_nic_tune "hybrid" "$CONFIG_SRC"
            enable_tune_service "hybrid-tune.service" "$OPT_DIR/scripts/hybrid-tune.sh"
            ;;
        rps)
            install_nic_tune "rps" "$CONFIG_SRC"
            enable_tune_service "network-tune.service" "$OPT_DIR/scripts/network-tune.sh"
            ;;
    esac

    log_info "Verifying optimizations..."
    local verify_ok=true

    if [ "$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)" != "bbr" ]; then
        log_warn "BBR not active (kernel may not support it)"
        verify_ok=false
    fi

    local hashsize expected_hashsize=524288
    [ "$opt_profile" = "panel" ] && expected_hashsize=32768
    hashsize=$(cat /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || echo "0")
    if [ "$hashsize" -lt "$expected_hashsize" ] 2>/dev/null; then
        log_warn "Conntrack hashsize is $hashsize (expected >=$expected_hashsize)"
        verify_ok=false
    fi

    if [ "$verify_ok" = true ]; then
        log_success "All optimizations verified successfully"
    fi

    log_success "$(msg optimizations_applied)"
}

check_optimizations_status() {
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        local profile="vpn"
        [ -f /opt/monitoring/configs/OPT_PROFILE ] && profile=$(cat /opt/monitoring/configs/OPT_PROFILE 2>/dev/null || echo "vpn")
        echo "$(msg applied) ($profile)"
    else
        echo "$(msg not_applied)"
    fi
}

# ==================== Proxy Functions ====================

get_proxy_display() {
    local conf="/etc/monitoring/proxy.conf"
    [ -f "$conf" ] || return 0
    . "$conf" 2>/dev/null || return 0
    [ "$PROXY_ENABLED" = "1" ] && [ -n "$PROXY_URL" ] || return 0
    echo "$PROXY_URL" | sed -E 's|(://[^:]+):[^@]+@|\1:***@|'
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

remove_proxy_configs() {
    rm -f /etc/apt/apt.conf.d/99monitoring-proxy 2>/dev/null || true
    rm -f /etc/apt/apt.conf.d/99proxy 2>/dev/null || true
    rm -f /etc/systemd/system/docker.service.d/proxy.conf 2>/dev/null || true
    git config --global --unset http.proxy 2>/dev/null || true
    git config --global --unset https.proxy 2>/dev/null || true
    if command -v docker &>/dev/null; then
        timeout 60 systemctl daemon-reload >/dev/null 2>&1 || true
        timeout 60 systemctl restart docker >/dev/null 2>&1 || true
    fi
}

setup_proxy() {
    echo ""
    echo -e "  ${CYAN}══ $(msg menu_configure_proxy) ══${NC}"
    echo ""

    local current_display
    current_display=$(get_proxy_display)
    if [ -n "$current_display" ]; then
        echo -e "  $(msg proxy_current): ${GREEN}${current_display}${NC}"
        echo ""
    fi
    echo -e "  ${YELLOW}$(msg proxy_empty_disable)${NC}"
    echo ""

    local proxy_addr
    proxy_addr=$(safe_read "  $(msg proxy_enter_address): " "" "$TIMEOUT_USER_INPUT")

    if [ -z "$proxy_addr" ]; then
        mkdir -p /etc/monitoring 2>/dev/null || true
        cat > /etc/monitoring/proxy.conf << 'PROXYEOF'
PROXY_ENABLED=0
PROXY_URL=
PROXYEOF
        remove_proxy_configs
        unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY
        log_info "$(msg proxy_removed)"
        return 0
    fi

    local proxy_url
    if echo "$proxy_addr" | grep -qE '^https?://'; then
        proxy_url="$proxy_addr"
    else
        proxy_url="http://${proxy_addr}"
    fi

    local needs_auth
    needs_auth=$(safe_read "  $(msg proxy_auth_prompt): " "n" 30)

    if [ "$needs_auth" = "y" ] || [ "$needs_auth" = "Y" ]; then
        local proxy_user proxy_pass
        proxy_user=$(safe_read "  $(msg proxy_enter_user): " "" 60)
        proxy_pass=$(safe_read "  $(msg proxy_enter_pass): " "" 60)
        if [ -n "$proxy_user" ] && [ -n "$proxy_pass" ]; then
            proxy_url=$(echo "$proxy_url" | sed -E "s|^(https?://)(.*)|\1${proxy_user}:${proxy_pass}@\2|")
        fi
    fi

    mkdir -p /etc/monitoring 2>/dev/null || true
    cat > /etc/monitoring/proxy.conf << PROXYEOF
PROXY_ENABLED=1
PROXY_URL=${proxy_url}
PROXYEOF
    chmod 600 /etc/monitoring/proxy.conf 2>/dev/null || true

    load_proxy
    configure_apt_proxy
    configure_docker_proxy

    log_info "$(msg proxy_testing)"
    if timeout 15 curl -fsSL --connect-timeout 10 --max-time 15 "https://github.com" >/dev/null 2>&1; then
        log_success "$(msg proxy_test_ok)"
        log_success "$(msg proxy_configured)"
    else
        log_error "$(msg proxy_test_fail)"
        local save_anyway
        save_anyway=$(safe_read "  $(msg proxy_save_anyway): " "n" 30)
        if [ "$save_anyway" = "y" ] || [ "$save_anyway" = "Y" ]; then
            log_warn "$(msg proxy_configured)"
        else
            cat > /etc/monitoring/proxy.conf << 'PROXYEOF'
PROXY_ENABLED=0
PROXY_URL=
PROXYEOF
            remove_proxy_configs
            unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY
            log_info "$(msg proxy_not_saved)"
        fi
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

# ==================== DNS Configuration ====================

configure_dns() {
    local DNS_PRIMARY="1.1.1.1"
    local DNS_SECONDARY="8.8.8.8"
    local DNS_FALLBACK1="1.0.0.1"
    local DNS_FALLBACK2="8.8.4.4"

    # Известные публичные DNS которые хостеры/DHCP обычно ставят (escaped for grep -E)
    local KNOWN_DNS_PATTERN="8\.8\.8\.8|8\.8\.4\.4|208\.67\.222\.222|208\.67\.220\.220|77\.88\.8\.8|77\.88\.8\.1|9\.9\.9\.9|149\.112\.112\.112|4\.2\.2\.1|4\.2\.2\.2|1\.1\.1\.1|1\.0\.0\.1"

    log_info "Configuring DNS ($DNS_PRIMARY, $DNS_SECONDARY)..."

    # ── Шаг 1: systemd-resolved drop-in ──
    if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/dns.conf << EOF
[Resolve]
DNS=$DNS_PRIMARY $DNS_SECONDARY
FallbackDNS=$DNS_FALLBACK1 $DNS_FALLBACK2
EOF
        systemctl restart systemd-resolved 2>/dev/null || true
        log_success "systemd-resolved: configured"
    fi

    # ── Шаг 2: Netplan (если есть) ──
    local netplan_changed=0
    for yaml_file in /etc/netplan/*.yaml /etc/netplan/*.yml; do
        [ -f "$yaml_file" ] || continue
        grep -qE "($KNOWN_DNS_PATTERN)" "$yaml_file" || continue

        cp "$yaml_file" "${yaml_file}.dns-backup"

        local tmp_file="${yaml_file}.dns-tmp"
        cp "$yaml_file" "$tmp_file"

        local dns_index=0
        while IFS= read -r line; do
            if echo "$line" | grep -qE "^\s+- ($KNOWN_DNS_PATTERN)\s*$"; then
                dns_index=$((dns_index + 1))
                local indent
                indent=$(echo "$line" | sed 's/- .*/- /')
                if [ $dns_index -eq 1 ]; then
                    echo "${indent}${DNS_PRIMARY}"
                elif [ $dns_index -eq 2 ]; then
                    echo "${indent}${DNS_SECONDARY}"
                fi
            else
                echo "$line"
            fi
        done < "$tmp_file" > "$yaml_file"
        rm -f "$tmp_file"

        if [ $dns_index -eq 1 ]; then
            local last_indent
            last_indent=$(grep -E "^\s+- ${DNS_PRIMARY//./\\.}" "$yaml_file" | head -1 | sed 's/- .*/- /')
            sed -i "/- ${DNS_PRIMARY//./\\.}/a\\${last_indent}${DNS_SECONDARY}" "$yaml_file"
        fi

        netplan_changed=1
    done

    if [ "$netplan_changed" = "1" ]; then
        if netplan generate 2>/dev/null; then
            netplan apply 2>/dev/null || true
            log_success "Netplan: DNS updated"
            rm -f /etc/netplan/*.dns-backup 2>/dev/null || true
        else
            log_warn "Netplan: validation failed, rolling back"
            for backup in /etc/netplan/*.dns-backup; do
                [ -f "$backup" ] || continue
                mv "$backup" "${backup%.dns-backup}"
            done
        fi
    fi

    # ── Шаг 3: /etc/resolv.conf напрямую (если НЕ symlink на resolved) ──
    if [ -f /etc/resolv.conf ] && [ ! -L /etc/resolv.conf ]; then
        cp /etc/resolv.conf /etc/resolv.conf.dns-backup

        sed -i -E "s/^nameserver\s+($KNOWN_DNS_PATTERN)\s*$/nameserver __REPLACED__/" /etc/resolv.conf

        local count=0
        local tmp_resolv="/etc/resolv.conf.dns-tmp"
        while IFS= read -r line; do
            if [ "$line" = "nameserver __REPLACED__" ]; then
                count=$((count + 1))
                if [ $count -eq 1 ]; then
                    echo "nameserver $DNS_PRIMARY"
                elif [ $count -eq 2 ]; then
                    echo "nameserver $DNS_SECONDARY"
                fi
            else
                echo "$line"
            fi
        done < /etc/resolv.conf > "$tmp_resolv"
        mv "$tmp_resolv" /etc/resolv.conf

        if [ $count -eq 1 ]; then
            sed -i "/nameserver ${DNS_PRIMARY//./\\.}/a\\nameserver $DNS_SECONDARY" /etc/resolv.conf
        fi

        log_success "resolv.conf: DNS updated"
    elif [ -L /etc/resolv.conf ]; then
        log_info "resolv.conf: symlink (managed by resolved), skipping"
    fi

    # ── Шаг 4: dhclient.conf (чтобы DHCP не перезаписывал DNS) ──
    if [ -d /etc/dhcp ] && command -v dhclient &>/dev/null; then
        local dhclient_conf="/etc/dhcp/dhclient.conf"
        local dns_line="prepend domain-name-servers $DNS_PRIMARY, $DNS_SECONDARY;"
        if [ -f "$dhclient_conf" ]; then
            if ! grep -q "prepend domain-name-servers.*${DNS_PRIMARY}" "$dhclient_conf" 2>/dev/null; then
                sed -i '/^prepend domain-name-servers/d' "$dhclient_conf"
                echo "$dns_line" >> "$dhclient_conf"
                log_success "dhclient.conf: DNS prepend added"
            fi
        else
            echo "$dns_line" > "$dhclient_conf"
            log_success "dhclient.conf: created with DNS prepend"
        fi
    fi

    # ── Проверка ──
    local global_dns
    global_dns=$(resolvectl status 2>/dev/null | awk '/^Global/,/^Link/{if(/Current DNS Server:/) print $NF}' || true)
    if [ "$global_dns" = "$DNS_PRIMARY" ]; then
        log_success "DNS verified: $DNS_PRIMARY"
    elif [ -n "$global_dns" ]; then
        log_success "DNS configured (global: $global_dns, drop-in: $DNS_PRIMARY)"
    else
        local resolv_dns
        resolv_dns=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf 2>/dev/null || true)
        if [ "$resolv_dns" = "$DNS_PRIMARY" ]; then
            log_success "DNS verified: $DNS_PRIMARY"
        elif [ "$resolv_dns" = "127.0.0.53" ]; then
            log_success "DNS configured via systemd-resolved ($DNS_PRIMARY)"
        elif [ -n "$resolv_dns" ]; then
            log_warn "DNS: $resolv_dns (drop-in active, will use $DNS_PRIMARY)"
        fi
    fi
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
        apt_update_safe || log_warn "apt update had issues"
        apt_install_safe haproxy || {
            log_error "Failed to install HAProxy"
            return 1
        }
        # Fresh install — stop and disable: config is empty, user will enable via panel
        timeout "$TIMEOUT_SYSTEMCTL" systemctl stop haproxy >/dev/null 2>&1 || true
        timeout "$TIMEOUT_SYSTEMCTL" systemctl disable haproxy >/dev/null 2>&1 || true
        log_success "HAProxy installed (stopped, will start when configured via panel)"
    else
        log_success "HAProxy already installed"
    fi

    # Install ipset
    if ! command -v ipset &>/dev/null; then
        apt_install_safe ipset || log_warn "ipset installation had issues"
    else
        log_success "ipset already installed"
    fi
    
    mkdir -p /etc/haproxy 2>/dev/null || true

    configure_dns

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

# ==================== Remnawave Installation ====================

install_docker_if_needed() {
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        return 0
    fi

    log_info "$(msg remnawave_docker_installing)"

    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null 2>&1 || {
        log_error "Failed to update package list"
        return 1
    }

    apt-get install -y ca-certificates curl wget unzip ufw >/dev/null 2>&1 || {
        log_error "Failed to install base packages"
        return 1
    }

    if ! curl -fsSL https://get.docker.com -o /tmp/get-docker.sh; then
        log_error "$(msg remnawave_docker_error)"
        return 1
    fi
    if ! sh /tmp/get-docker.sh >/dev/null 2>&1; then
        log_error "$(msg remnawave_docker_error)"
        return 1
    fi
    rm -f /tmp/get-docker.sh

    systemctl start docker >/dev/null 2>&1 || true
    systemctl enable docker >/dev/null 2>&1 || true

    if ! docker info >/dev/null 2>&1; then
        log_error "$(msg remnawave_docker_error)"
        return 1
    fi

    ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true

    log_success "Docker installed"
}

randomhtml_remnawave() {
    cd /opt/ || { log_error "Cannot cd to /opt"; return 1; }

    if ! command -v unzip >/dev/null 2>&1; then
        apt-get install -y -qq unzip >/dev/null 2>&1 || { log_error "Failed to install unzip"; return 1; }
    fi

    rm -f main.zip 2>/dev/null
    rm -rf sni-templates-main/ nothing-sni-main/ 2>/dev/null

    log_info "$(msg remnawave_downloading_template)"

    local template_urls=(
        "https://github.com/distillium/sni-templates/archive/refs/heads/main.zip"
        "https://github.com/prettyleaf/nothing-sni/archive/refs/heads/main.zip"
    )

    local selected_url=${template_urls[$RANDOM % ${#template_urls[@]}]}

    local attempts=0
    while ! wget -q --timeout=30 --tries=3 --retry-connrefused "$selected_url" -O main.zip 2>/dev/null; do
        ((attempts++))
        if [ $attempts -ge 5 ]; then
            log_error "$(msg remnawave_template_error)"
            return 1
        fi
        sleep 3
    done

    unzip -o main.zip >/dev/null 2>&1 || { log_error "$(msg remnawave_template_error)"; rm -f main.zip; return 1; }
    rm -f main.zip

    local work_dir=""
    if [[ "$selected_url" == *"nothing-sni"* ]]; then
        work_dir="nothing-sni-main"
        cd "$work_dir" || { log_error "$(msg remnawave_template_error)"; return 1; }
        rm -rf .github README.md 2>/dev/null
    else
        work_dir="sni-templates-main"
        cd "$work_dir" || { log_error "$(msg remnawave_template_error)"; return 1; }
        rm -rf assets "README.md" "index.html" 2>/dev/null
    fi

    local RandomHTML
    if [[ "$selected_url" == *"nothing-sni"* ]]; then
        local selected_number=$((RANDOM % 8 + 1))
        RandomHTML="${selected_number}.html"
    else
        mapfile -t templates < <(find . -maxdepth 1 -type d -not -path . | sed 's|./||')
        RandomHTML="${templates[$RANDOM % ${#templates[@]}]}"
    fi

    if [[ "$selected_url" == *"distillium"* && "$RandomHTML" == "503 error pages" ]]; then
        cd "$RandomHTML" || { log_error "$(msg remnawave_template_error)"; return 1; }
        local versions=("v1" "v2")
        local RandomVersion="${versions[$RANDOM % ${#versions[@]}]}"
        RandomHTML="$RandomHTML/$RandomVersion"
        cd ..
    fi

    local random_meta_id=$(openssl rand -hex 16)
    local random_comment=$(openssl rand -hex 8)
    local random_class_suffix=$(openssl rand -hex 4)
    local random_title_suffix=$(openssl rand -hex 4)
    local random_footer_text="Designed by Site_${random_title_suffix}"
    local random_id_suffix=$(openssl rand -hex 4)

    local meta_names=("viewport-id" "session-id" "track-id" "render-id" "page-id" "config-id")
    local meta_usernames=("Payee6296" "UserX1234" "AlphaBeta" "GammaRay" "DeltaForce" "EchoZulu" "Foxtrot99" "HotelCalifornia" "IndiaInk" "JulietBravo")
    local random_meta_name=${meta_names[$RANDOM % ${#meta_names[@]}]}
    local random_username=${meta_usernames[$RANDOM % ${#meta_usernames[@]}]}

    local class_prefixes=("style" "data" "ui" "layout" "theme" "view")
    local random_class_prefix=${class_prefixes[$RANDOM % ${#class_prefixes[@]}]}
    local random_class="$random_class_prefix-$random_class_suffix"
    local random_title="Page_${random_title_suffix}"

    find "./$RandomHTML" -type f -name "*.html" -exec sed -i \
        -e "s|<!-- Website template by freewebsitetemplates.com -->||" \
        -e "s|<!-- Theme by: WebThemez.com -->||" \
        -e "s|<a href=\"http://freewebsitetemplates.com\">Free Website Templates</a>|<span>${random_footer_text}</span>|" \
        -e "s|<a href=\"http://webthemez.com\" alt=\"webthemez\">WebThemez.com</a>|<span>${random_footer_text}</span>|" \
        -e "s|id=\"Content\"|id=\"rnd_${random_id_suffix}\"|" \
        -e "s|id=\"subscribe\"|id=\"sub_${random_id_suffix}\"|" \
        -e "s|<title>.*</title>|<title>${random_title}</title>|" \
        -e "s/<\/head>/<meta name=\"$random_meta_name\" content=\"$random_meta_id\">\n<!-- $random_comment -->\n<\/head>/" \
        -e "s/<body/<body class=\"$random_class\"/" \
        -e "s/CHANGEMEPLS/$random_username/g" \
        {} \;

    find "./$RandomHTML" -type f -name "*.css" -exec sed -i \
        -e "1i\/* $random_comment */" \
        -e "1i.$random_class { display: block; }" \
        {} \;

    if [[ -d "${RandomHTML}" ]]; then
        mkdir -p "/var/www/html/"
        rm -rf /var/www/html/*
        cp -a "${RandomHTML}"/. "/var/www/html/"
    elif [[ -f "${RandomHTML}" ]]; then
        mkdir -p "/var/www/html/"
        cp "${RandomHTML}" "/var/www/html/index.html"
    else
        log_error "$(msg remnawave_template_error)"
        cd /opt/
        rm -rf sni-templates-main/ nothing-sni-main/
        return 1
    fi

    cd /opt/
    rm -rf sni-templates-main/ nothing-sni-main/
    log_success "$(msg remnawave_template_applied)"
}

install_remnawave() {
    if [ -d "$REMNAWAVE_DIR" ] && [ -f "$REMNAWAVE_DIR/docker-compose.yml" ]; then
        log_warn "$(msg remnawave_already_installed) $REMNAWAVE_DIR"
        local confirm
        confirm=$(safe_read "$(msg remnawave_reinstall_confirm) " "n" 30)
        if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
            log_info "$(msg installation_cancelled)"
            return 1
        fi
        log_info "$(msg stopping_containers)"
        (cd "$REMNAWAVE_DIR" && timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down -v >/dev/null 2>&1) || true
        rm -rf "$REMNAWAVE_DIR"
    fi

    install_docker_if_needed || return 1

    echo ""
    log_info "$(msg remnawave_enter_key)"
    local CERTIFICATE=""
    while IFS= read -r line; do
        if [ -z "$line" ]; then
            [ -n "$CERTIFICATE" ] && break
        else
            CERTIFICATE="${CERTIFICATE}${line}\n"
        fi
    done </dev/tty

    log_info "$(msg remnawave_installing)"

    mkdir -p "$REMNAWAVE_DIR"
    mkdir -p "$REMNAWAVE_DIR/ssl"

    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -days 3650 -nodes \
        -keyout "$REMNAWAVE_DIR/ssl/privkey.pem" \
        -out "$REMNAWAVE_DIR/ssl/fullchain.pem" \
        -subj "/CN=localhost" >/dev/null 2>&1 || {
        log_error "Failed to generate self-signed certificate"
        return 1
    }

    cat > "$REMNAWAVE_DIR/docker-compose.yml" <<'COMPOSE_HEAD'
services:
  remnawave-nginx:
    image: nginx:1.28
    container_name: remnawave-nginx
    hostname: remnawave-nginx
    restart: always
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
      - /dev/shm:/dev/shm:rw
      - /var/www/html:/var/www/html:ro
    command: sh -c 'rm -f /dev/shm/nginx.sock && exec nginx -g "daemon off;"'
    network_mode: host
    depends_on:
      - remnanode
    logging:
      driver: 'json-file'
      options:
        max-size: '30m'
        max-file: '5'

COMPOSE_HEAD

    cat >> "$REMNAWAVE_DIR/docker-compose.yml" <<COMPOSE_NODE
  remnanode:
    image: remnawave/node:latest
    container_name: remnanode
    hostname: remnanode
    restart: always
    ulimits:
      nofile:
        soft: 1048576
        hard: 1048576
    cap_add:
      - NET_ADMIN
    network_mode: host
    environment:
      - NODE_PORT=2222
      - SECRET_KEY=$(echo -e "$CERTIFICATE")
    volumes:
      - /dev/shm:/dev/shm:rw
    logging:
      driver: 'json-file'
      options:
        max-size: '30m'
        max-file: '5'
COMPOSE_NODE

    cat > "$REMNAWAVE_DIR/nginx.conf" <<'NGINX_CONF'
server_names_hash_bucket_size 64;

map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

ssl_protocols TLSv1.2 TLSv1.3;
ssl_ecdh_curve X25519:prime256v1:secp384r1;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
ssl_prefer_server_ciphers on;
ssl_session_timeout 1d;
ssl_session_cache shared:MozSSL:10m;
ssl_session_tickets off;

server {
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol default_server;
    server_name _;
    http2 on;
    keepalive_timeout 5s;

    ssl_certificate /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;
    ssl_trusted_certificate /etc/nginx/ssl/fullchain.pem;

    root /var/www/html;
    index index.html;
    add_header X-Robots-Tag "noindex, nofollow, noarchive, nosnippet, noimageindex" always;
}
NGINX_CONF

    randomhtml_remnawave

    log_info "$(msg remnawave_starting)"
    (cd "$REMNAWAVE_DIR" && docker compose up -d >/dev/null 2>&1) || {
        log_error "Failed to start containers"
        return 1
    }

    sleep 3
    if docker ps --format '{{.Names}}' | grep -q "remnanode"; then
        log_success "$(msg remnawave_installed)"
        local server_ip
        server_ip=$(curl -s --max-time 5 https://ifconfig.me 2>/dev/null \
            || curl -s --max-time 5 https://icanhazip.com 2>/dev/null \
            || curl -s --max-time 5 https://ident.me 2>/dev/null \
            || ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
        if [ -n "$server_ip" ]; then
            echo ""
            log_info "$(msg remnawave_server_ip): ${GREEN}${server_ip}${NC}"
        fi
    else
        log_warn "Containers may not have started correctly. Check: docker ps"
    fi
}

# ==================== Language Selection ====================

select_language() {
    printf "\033[0m" 2>/dev/null
    clear 2>/dev/null || printf "\033[2J\033[H" 2>/dev/null

    local title="Language / Язык"
    local tlen=${#title}
    local box_w=$(( tlen + 6 ))
    [ $box_w -lt 30 ] && box_w=30
    local border=""
    local j; for ((j=0; j<box_w; j++)); do border+="═"; done
    local pl=$(( (box_w - tlen) / 2 ))
    local pr=$(( box_w - tlen - pl ))

    echo ""
    echo -e "  ${CYAN}╔${border}╗${NC}"
    printf "  ${CYAN}║${NC}%*s%s%*s${CYAN}║${NC}\n" "$pl" "" "$title" "$pr" ""
    echo -e "  ${CYAN}╚${border}╝${NC}"
    echo ""
    echo -e "  ${GREEN}1)${NC} English"
    echo -e "  ${GREEN}2)${NC} Русский"
    echo ""

    local lang_choice
    lang_choice=$(safe_read "  Select / Выберите [1-2]: " "1" 30)

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

# ==================== WARP ====================

WARP_PORT=9091

install_warp() {
    # Проверка: уже установлен?
    if command -v warp-cli &>/dev/null; then
        log_warn "$(msg warp_already_installed): $(warp-cli --version 2>/dev/null || echo '?')"
        local answer
        answer=$(safe_read "$(msg warp_reinstall_confirm) " "n" 30)
        [[ "$answer" =~ ^[Yy]$ ]] || return 0
    fi

    log_info "$(msg warp_installing)"

    # Ждём освобождения apt
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
        sleep 2
    done

    # Репозиторий Cloudflare
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | \
        gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg 2>/dev/null

    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] \
https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | \
        tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null

    apt-get update -qq >/dev/null
    apt-get install -y cloudflare-warp >/dev/null

    log_success "Cloudflare WARP: $(warp-cli --version 2>/dev/null || echo 'installed')"

    # Фикс для VPS с /32 адресацией: WARP не видит primary IPv4 без LAN range
    fix_warp_network

    # Ждём полной инициализации сервиса
    sleep 5

    # Регистрация + настройка
    log_info "$(msg warp_registering)"
    warp-cli --accept-tos registration delete &>/dev/null || true
    local reg_out
    reg_out=$(warp-cli --accept-tos registration new 2>&1) || true
    if echo "$reg_out" | grep -qi "success"; then
        log_success "Registration OK"
    else
        log_warn "Registration: $reg_out"
        sleep 3
        reg_out=$(warp-cli --accept-tos registration new 2>&1) || true
        log_warn "Retry: $reg_out"
    fi

    local mode_out port_out
    mode_out=$(warp-cli --accept-tos mode proxy 2>&1) || true
    port_out=$(warp-cli --accept-tos proxy port "$WARP_PORT" 2>&1) || true
    [ "$mode_out" != "Success" ] && log_warn "Mode: $mode_out"
    [ "$port_out" != "Success" ] && log_warn "Port: $port_out"

    # Подключение
    log_info "$(msg warp_connecting)"
    warp-cli --accept-tos connect &>/dev/null

    local connected=false
    for i in {1..15}; do
        if warp-cli --accept-tos status 2>/dev/null | grep -qi "connected"; then
            connected=true
            break
        fi
        sleep 2
    done

    if [ "$connected" = true ]; then
        log_success "$(msg warp_connected)"
    else
        local fail_status
        fail_status=$(warp-cli --accept-tos status 2>&1 | grep -E "Status|Reason" | head -2)
        log_warn "$(msg warp_connect_timeout)"
        log_warn "$fail_status"
    fi

    # Скрипт фикса /32 для автозапуска
    tee /usr/local/bin/warp-fix-network.sh >/dev/null << 'FIXSCRIPT'
#!/bin/bash
iface=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
[ -z "$iface" ] && exit 0
prefix=$(ip -4 addr show dev "$iface" 2>/dev/null | awk '/inet / {split($2,a,"/"); print a[2]}' | head -1)
if [ "$prefix" = "32" ] || [ -z "$prefix" ]; then
    ip addr add 172.30.255.1/24 dev "$iface" 2>/dev/null || true
    systemctl restart warp-svc
    sleep 5
fi
FIXSCRIPT
    chmod +x /usr/local/bin/warp-fix-network.sh

    # Автозапуск
    tee /etc/systemd/system/warp-auto.service >/dev/null << 'SYSTEMD'
[Unit]
Description=Cloudflare WARP auto-connect
After=network.target warp-svc.service
Requires=warp-svc.service

[Service]
Type=oneshot
ExecStartPre=/usr/local/bin/warp-fix-network.sh
ExecStart=/usr/bin/warp-cli connect
RemainAfterExit=yes
ExecStop=/usr/bin/warp-cli disconnect

[Install]
WantedBy=multi-user.target
SYSTEMD

    systemctl daemon-reload
    systemctl enable warp-auto &>/dev/null
    log_success "$(msg warp_autostart_configured)"

    # Проверка прокси (даём время на установку соединения)
    sleep 2
    local warp_ip
    warp_ip=$(curl -s --max-time 10 --socks5 "127.0.0.1:${WARP_PORT}" https://cloudflare.com/cdn-cgi/trace 2>/dev/null | grep "^ip=" | cut -d= -f2)

    echo ""
    if [ -n "$warp_ip" ]; then
        log_success "$(msg warp_proxy_ok) — IP: ${warp_ip}"
    else
        log_warn "$(msg warp_proxy_fail)"
    fi

    log_success "$(msg warp_installed)"
    echo -e "  SOCKS5: ${GREEN}127.0.0.1:${WARP_PORT}${NC}"
    echo ""
    echo '  xray outbound:'
    echo '  {'
    echo '    "tag": "warp",'
    echo '    "protocol": "socks",'
    echo '    "settings": {'
    echo '      "address": "127.0.0.1",'
    echo '      "port": '"${WARP_PORT}"
    echo '    }'
    echo '  }'
    echo ""
}

fix_warp_network() {
    local iface prefix
    iface=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
    [ -z "$iface" ] && return 0

    prefix=$(ip -4 addr show dev "$iface" 2>/dev/null | awk '/inet / {split($2,a,"/"); print a[2]}' | head -1)

    # /32 или нет IPv4 на базовом интерфейсе — WARP не увидит primary interface
    if [ "$prefix" = "32" ] || [ -z "$prefix" ]; then
        log_info "VPS /32 fix: 172.30.255.1/24 → $iface"
        ip addr add 172.30.255.1/24 dev "$iface" 2>/dev/null || true
        systemctl restart warp-svc &>/dev/null
        sleep 8
    fi
}

# ==================== Speed Test ====================

ensure_snapd() {
    if command -v snap &>/dev/null; then
        return 0
    fi

    log_info "$(msg speedtest_installing_snapd)"
    apt_update_safe || log_warn "apt update had issues"
    if ! apt_install_safe snapd; then
        log_error "$(msg speedtest_snapd_failed)"
        return 1
    fi

    systemctl enable --now snapd.socket &>/dev/null || true
    systemctl enable --now snapd.service &>/dev/null || true

    local waited=0
    while [ $waited -lt 30 ]; do
        if snap wait system seed.loaded &>/dev/null; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done

    command -v snap &>/dev/null && return 0
    log_error "$(msg speedtest_snapd_failed)"
    return 1
}

ensure_speedtest_snap() {
    local bin
    bin=$(command -v speedtest 2>/dev/null || true)
    if [ -n "$bin" ] && [[ "$bin" == /snap/* ]]; then
        return 0
    fi

    if dpkg -l speedtest-cli 2>/dev/null | grep -q '^ii'; then
        log_info "$(msg speedtest_removing_cli)"
        env DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq speedtest-cli >/dev/null 2>&1 || true
    fi

    ensure_snapd || return 1

    log_info "$(msg speedtest_installing_speedtest)"
    if ! spin_retry 300 "$MAX_RETRIES" "$RETRY_DELAY" "snap install speedtest" \
        snap install speedtest; then
        log_error "$(msg speedtest_snap_failed)"
        return 1
    fi

    /snap/bin/speedtest --accept-license --accept-gdpr --version &>/dev/null || true
    return 0
}

ensure_iperf_deps() {
    local missing=()
    command -v iperf3 &>/dev/null || missing+=("iperf3")
    command -v jq     &>/dev/null || missing+=("jq")
    command -v bc     &>/dev/null || missing+=("bc")
    command -v ping   &>/dev/null || missing+=("iputils-ping")

    if [ ${#missing[@]} -eq 0 ]; then
        return 0
    fi

    log_info "$(msg speedtest_installing_iperf_deps)"
    apt_update_safe || log_warn "apt update had issues"
    if ! apt_install_safe "${missing[@]}"; then
        log_error "$(msg speedtest_iperf_deps_failed)"
        return 1
    fi
    return 0
}

run_speedtest_ookla() {
    ensure_speedtest_snap || return 1
    log_info "$(msg speedtest_ookla_running)"
    echo ""
    /snap/bin/speedtest --accept-license --accept-gdpr || true
    echo ""
}

# ---------- iperf3 core (adapted from github.com/itdoginfo/russian-iperf3-servers) ----------

_iperf_log_debug() {
    [ "${_IPERF_DEBUG:-false}" = true ] || return 0
    if [ -n "${_IPERF_SPINNER_PID:-}" ]; then
        echo -e "\n\e[37m[DEBUG] $1\e[0m" >&2
    else
        echo -e "\e[37m[DEBUG] $1\e[0m" >&2
    fi
}

_iperf_start_spinner() {
    local message="$1"
    echo -n "$message"
    (
        local chars=("⠇" "⠏" "⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧")
        local i=0
        while true; do
            printf "\r%s %s" "$message" "${chars[$i]}"
            i=$(( (i + 1) % ${#chars[@]} ))
            sleep 0.15
        done
    ) &
    _IPERF_SPINNER_PID=$!
}

_iperf_stop_spinner() {
    local result="$1"
    [ -n "${_IPERF_SPINNER_PID:-}" ] && kill "$_IPERF_SPINNER_PID" 2>/dev/null
    printf "\r\033[K"
    [ "${_IPERF_DEBUG:-false}" = true ] && echo "$result"
    unset _IPERF_SPINNER_PID
}

_iperf_find_port() {
    local host="$1"
    local port
    for port in "${_IPERF_PORT_RANGE[@]}"; do
        _iperf_log_debug "Trying $host:$port"
        local out
        out=$(timeout "$_IPERF_TIMEOUT" iperf3 -c "$host" -p "$port" -t 1 2>&1 || echo "")
        if [[ "$out" == *"receiver"* && "$out" != *"error"* ]]; then
            echo "$port"
            return 0
        fi
    done
    return 1
}

_iperf_test_server() {
    local host="$1" port="$2" streams="$3"
    local out
    out=$(timeout "$_IPERF_TIMEOUT" iperf3 -c "$host" -p "$port" -P "$streams" -t "$_IPERF_TEST_DURATION" -J 2>/dev/null || echo "")
    if [[ -n "$out" && "$out" == *'"receiver"'* && "${#out}" -gt 50 ]]; then
        echo "$out"
        return 0
    fi
    return 1
}

_iperf_parse_speed() {
    local json="$1" direction="$2"
    if [ "$direction" = "sender" ]; then
        echo "$json" | jq -r ".end.sum_sent.bits_per_second // 0" | awk '{printf "%.1f", $1/1000000}'
    else
        echo "$json" | jq -r ".end.sum_received.bits_per_second // 0" | awk '{printf "%.1f", $1/1000000}'
    fi
}

_iperf_get_ping() {
    local host="$1"
    ping -c 5 -W 2 "$host" 2>/dev/null | grep -oP 'rtt min/avg/max/mdev = [0-9.]+/\K[0-9]+' || echo "N/A"
}

_iperf_process_result() {
    local result="$1" city="$2" host="$3" port="$4" is_fallback="${5:-false}"
    local download upload ping_ms
    download=$(_iperf_parse_speed "$result" "receiver")
    upload=$(_iperf_parse_speed "$result" "sender")
    ping_ms=$(_iperf_get_ping "$host")

    if [ "$download" != "0.0" ] || [ "$upload" != "0.0" ]; then
        local display="$city"
        [ "$is_fallback" = "true" ] && display="$city (F)"
        _iperf_stop_spinner "Testing $city ($host:$port)... ✓"
        _IPERF_RESULTS+=("$(printf "%-18s %-15s %-15s %-10s" "$display" "${download} Mbps" "${upload} Mbps" "${ping_ms} ms")")
        return 0
    fi
    return 1
}

_iperf_test_city() {
    local city="$1" host="$2" fallback_host="$3"
    local fallback_city="${_IPERF_FALLBACK_CITIES[$city]}"

    _iperf_start_spinner "Testing $city ($host)..."

    local port result
    if port=$(_iperf_find_port "$host"); then
        if result=$(_iperf_test_server "$host" "$port" "$_IPERF_PARALLEL_STREAMS"); then
            _iperf_process_result "$result" "$city" "$host" "$port" && return 0
        fi
        if result=$(_iperf_test_server "$host" "$port" "$_IPERF_FALLBACK_STREAMS"); then
            _iperf_process_result "$result" "$city" "$host" "$port" && return 0
        fi
    fi

    _iperf_log_debug "Primary failed, trying fallback $fallback_host"
    local fport
    if fport=$(_iperf_find_port "$fallback_host"); then
        if result=$(_iperf_test_server "$fallback_host" "$fport" "$_IPERF_PARALLEL_STREAMS"); then
            _iperf_process_result "$result" "$fallback_city" "$fallback_host" "$fport" "true" && return 0
        fi
        if result=$(_iperf_test_server "$fallback_host" "$fport" "$_IPERF_FALLBACK_STREAMS"); then
            _iperf_process_result "$result" "$fallback_city" "$fallback_host" "$fport" "true" && return 0
        fi
    fi

    _iperf_stop_spinner "Testing $city ($host)... ✗"
    _IPERF_RESULTS+=("$(printf "%-18s %-15s %-15s %-10s" "$city" "\e[31m-\e[0m" "\e[31m-\e[0m" "N/A")")
    return 1
}

_iperf_print_results() {
    echo
    printf "%-18s %-15s %-15s %-10s\n" "Server" "Download" "Upload" "Ping"
    printf "%-18s %-15s %-15s %-10s\n" "------" "--------" "------" "----"
    local r
    for r in "${_IPERF_RESULTS[@]}"; do
        echo -e "$r"
    done
}

_iperf_cleanup_trap() {
    [ -n "${_IPERF_SPINNER_PID:-}" ] && kill "$_IPERF_SPINNER_PID" 2>/dev/null
    printf "\r\033[K"
    unset _IPERF_SPINNER_PID
}

run_speedtest_iperf() {
    ensure_iperf_deps || return 1

    local count_input count
    count_input=$(safe_read "$(msg speedtest_iperf_count_prompt) (default 5): " "5" 60)
    if [[ "$count_input" =~ ^[0-9]+$ ]] && [ "$count_input" -ge 1 ] && [ "$count_input" -le 10 ]; then
        count=$count_input
    else
        count=5
    fi

    local _IPERF_DEBUG=false
    local _IPERF_TIMEOUT=15
    local _IPERF_TEST_DURATION=10
    local _IPERF_PARALLEL_STREAMS=8
    local _IPERF_FALLBACK_STREAMS=8
    local _IPERF_INTER_DELAY=1
    local _IPERF_PORT_RANGE=(5201 5202 5203 5204 5205 5206 5207 5208 5209)
    local _IPERF_RESULTS=()

    declare -A _IPERF_SERVERS=(
        ["Moscow"]="spd-rudp.hostkey.ru"
        ["Saint Petersburg"]="st.spb.ertelecom.ru"
        ["Nizhny Novgorod"]="st.nn.ertelecom.ru"
        ["Chelyabinsk"]="st.chel.ertelecom.ru"
        ["Tyumen"]="st.tmn.ertelecom.ru"
    )
    declare -A _IPERF_FALLBACK_SERVERS=(
        ["Moscow"]="st.tver.ertelecom.ru"
        ["Saint Petersburg"]="st.yar.ertelecom.ru"
        ["Nizhny Novgorod"]="speed-nn.vtt.net"
        ["Chelyabinsk"]="st.mgn.ertelecom.ru"
        ["Tyumen"]="st.krsk.ertelecom.ru"
    )
    declare -A _IPERF_FALLBACK_CITIES=(
        ["Moscow"]="Tver"
        ["Saint Petersburg"]="Yaroslavl"
        ["Nizhny Novgorod"]="Nizhny Novgorod"
        ["Chelyabinsk"]="Magnitogorsk"
        ["Tyumen"]="Krasnoyarsk"
    )
    local _IPERF_CITY_ORDER=("Moscow" "Saint Petersburg" "Nizhny Novgorod" "Chelyabinsk" "Tyumen")

    local available=${#_IPERF_CITY_ORDER[@]}
    if [ "$count" -gt "$available" ]; then
        log_info "$(msg speedtest_iperf_count_clamped) ${available}"
        count=$available
    fi

    log_info "$(msg speedtest_iperf_running)"
    local start_ts
    start_ts=$(date +%s)

    trap '_iperf_cleanup_trap' INT TERM

    local i=0
    for city in "${_IPERF_CITY_ORDER[@]}"; do
        [ "$i" -ge "$count" ] && break
        local server="${_IPERF_SERVERS[$city]}"
        local fallback="${_IPERF_FALLBACK_SERVERS[$city]}"
        _iperf_test_city "$city" "$server" "$fallback"
        i=$((i + 1))
        [ "$i" -lt "$count" ] && sleep "$_IPERF_INTER_DELAY"
    done

    trap cleanup EXIT
    trap 'echo ""; echo -e "\033[0;31m[ERROR] Interrupted by user (Ctrl+C)\033[0m"; exit 130' INT
    trap 'echo ""; echo -e "\033[0;31m[ERROR] Terminated by signal\033[0m"; exit 143' TERM

    _iperf_print_results
    local end_ts=$(date +%s)
    echo
    printf "\033[0;36mExecution time: %d seconds\033[0m\n" "$((end_ts - start_ts))"
    echo
}

run_speed_test_menu() {
    while true; do
        printf "\033[0m" 2>/dev/null
        clear 2>/dev/null || printf "\033[2J\033[H" 2>/dev/null

        local title
        title="$(msg speedtest_menu_title)"
        local tlen=${#title}
        local box_w=$(( tlen + 6 ))
        [ $box_w -lt 40 ] && box_w=40
        local border=""
        local j; for ((j=0; j<box_w; j++)); do border+="═"; done
        local pl=$(( (box_w - tlen) / 2 ))
        local pr=$(( box_w - tlen - pl ))

        echo ""
        echo -e "  ${CYAN}╔${border}╗${NC}"
        printf "  ${CYAN}║${NC}%*s%s%*s${CYAN}║${NC}\n" "$pl" "" "$title" "$pr" ""
        echo -e "  ${CYAN}╚${border}╝${NC}"
        echo ""
        echo -e "  ${GREEN}1)${NC} $(msg speedtest_opt_ookla)"
        echo -e "  ${GREEN}2)${NC} $(msg speedtest_opt_iperf)"
        echo ""
        echo -e "  ${YELLOW}0)${NC} $(msg speedtest_back)"
        echo ""

        local choice
        choice=$(safe_read "$(msg select_action): " "0" 60)

        case $choice in
            1)
                run_speedtest_ookla
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            2)
                run_speedtest_iperf
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            0)
                return 0
                ;;
            *)
                log_error "$(msg invalid_option)"
                sleep 1
                ;;
        esac
    done
}

# ==================== Menu ====================

show_menu() {
    # Reset terminal state after spinner or other operations
    printf "\033[0m" 2>/dev/null
    clear 2>/dev/null || printf "\033[2J\033[H" 2>/dev/null

    # Dynamic title box — adapts to any language
    local title
    title="$(msg menu_title)"
    local tlen=${#title}
    local box_w=$(( tlen + 6 ))
    [ $box_w -lt 40 ] && box_w=40
    local border=""
    local j; for ((j=0; j<box_w; j++)); do border+="═"; done
    local pl=$(( (box_w - tlen) / 2 ))
    local pr=$(( box_w - tlen - pl ))

    echo ""
    echo -e "  ${CYAN}╔${border}╗${NC}"
    printf "  ${CYAN}║${NC}%*s%s%*s${CYAN}║${NC}\n" "$pl" "" "$title" "$pr" ""
    echo -e "  ${CYAN}╚${border}╝${NC}"
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

    local panel_installed=false node_installed=false
    [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ] && panel_installed=true
    [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ] && node_installed=true

    if [ "$panel_installed" = true ] || [ "$node_installed" = true ]; then
        echo ""
    fi
    if [ "$panel_installed" = true ]; then
        echo -e "  ${RED}5)${NC} $(msg menu_remove_panel)"
    fi
    if [ "$node_installed" = true ]; then
        echo -e "  ${RED}6)${NC} $(msg menu_remove_node)"
    fi

    echo ""
    echo -e "  ${CYAN}7)${NC} $(msg menu_optimize_system)"
    echo -e "  ${CYAN}8)${NC} $(msg menu_configure_proxy)"
    echo -e "  ${CYAN}9)${NC} $(msg menu_install_remnawave)"
    echo -e "  ${CYAN}w)${NC} $(msg menu_install_warp)"
    echo -e "  ${CYAN}s)${NC} $(msg menu_speed_test)"
    echo ""
    echo -e "  ${YELLOW}0)${NC} $(msg menu_exit)"
    echo ""

    # Status section — short lines, no paths
    echo -e "  ${BLUE}$(msg status):${NC}"

    if [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ]; then
        local panel_version="?"
        [ -f "$PANEL_DIR/VERSION" ] && panel_version=$(cat "$PANEL_DIR/VERSION" 2>/dev/null || echo "?")
        echo -e "    Panel:  ${GREEN}$(msg installed)${NC} v${panel_version}"
    else
        echo -e "    Panel:  ${YELLOW}$(msg not_installed)${NC}"
    fi

    if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ]; then
        local node_version="?"
        [ -f "$NODE_DIR/VERSION" ] && node_version=$(cat "$NODE_DIR/VERSION" 2>/dev/null || echo "?")
        echo -e "    Node:   ${GREEN}$(msg installed)${NC} v${node_version}"
    else
        echo -e "    Node:   ${YELLOW}$(msg not_installed)${NC}"
    fi

    if [ -d "$REMNAWAVE_DIR" ] && [ -f "$REMNAWAVE_DIR/docker-compose.yml" ]; then
        echo -e "    Remna:  ${GREEN}$(msg installed)${NC}"
    else
        echo -e "    Remna:  ${YELLOW}$(msg not_installed)${NC}"
    fi

    local opt_version=""
    [ -f /opt/monitoring/configs/VERSION ] && opt_version=$(cat /opt/monitoring/configs/VERSION 2>/dev/null || echo "")
    [ -z "$opt_version" ] && [ -f /opt/monitoring-node/configs/VERSION ] && opt_version=$(cat /opt/monitoring-node/configs/VERSION 2>/dev/null || echo "")
    
    if [ -f /etc/sysctl.d/99-vless-tuning.conf ]; then
        if [ -n "$opt_version" ]; then
            echo -e "    Sysctl: ${GREEN}$(msg applied)${NC} v${opt_version}"
        else
            echo -e "    Sysctl: ${GREEN}$(msg applied)${NC}"
        fi
    else
        echo -e "    Sysctl: ${YELLOW}$(msg not_applied)${NC}"
    fi

    if timeout 5 systemctl is-enabled hybrid-tune.service &>/dev/null 2>&1; then
        echo -e "    NIC:    ${GREEN}hybrid ($(msg applied))${NC}"
    elif timeout 5 systemctl is-enabled multiqueue-tune.service &>/dev/null 2>&1; then
        echo -e "    NIC:    ${GREEN}multiqueue ($(msg applied))${NC}"
    elif timeout 5 systemctl is-enabled network-tune.service &>/dev/null 2>&1; then
        echo -e "    NIC:    ${GREEN}RPS ($(msg applied))${NC}"
    else
        echo -e "    NIC:    ${YELLOW}$(msg not_applied)${NC}"
    fi

    local proxy_display
    proxy_display=$(get_proxy_display)
    if [ -n "$proxy_display" ]; then
        echo -e "    $(msg proxy_status): ${GREEN}$(msg proxy_enabled)${NC} — ${proxy_display}"
    else
        echo -e "    $(msg proxy_status): ${YELLOW}$(msg proxy_disabled)${NC}"
    fi

    if command -v warp-cli &>/dev/null; then
        local warp_status
        warp_status=$(warp-cli --accept-tos status 2>/dev/null | grep -o "Connected" || echo "")
        if [ "$warp_status" = "Connected" ]; then
            echo -e "    WARP:   ${GREEN}connected${NC} — socks5://127.0.0.1:${WARP_PORT}"
        else
            echo -e "    WARP:   ${YELLOW}disconnected${NC}"
        fi
    else
        echo -e "    WARP:   ${YELLOW}$(msg not_installed)${NC}"
    fi
    echo ""
}

# ==================== Main ====================

main() {
    # Acquire lock to prevent parallel execution
    acquire_lock
    
    check_root
    load_language
    load_proxy
    
    # First run - select language
    if [ ! -f /etc/monitoring/language ]; then
        select_language
    fi
    
    # Ensure CLI command exists on every run
    if [ ! -f "$BIN_PATH" ]; then
        install_cli
    fi
    
    while true; do
        # Reset cwd — previous install/remove may have deleted current directory
        cd / 2>/dev/null || true
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
                if [ -d "$PANEL_DIR" ] && [ -f "$PANEL_DIR/docker-compose.yml" ]; then
                    remove_panel
                else
                    log_error "$(msg invalid_option)"
                    sleep 1
                fi
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            6)
                if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/docker-compose.yml" ]; then
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
            8)
                setup_proxy
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            9)
                install_remnawave
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            w|W)
                install_warp
                safe_read "$(msg press_enter)" "" 30 >/dev/null
                ;;
            s|S)
                run_speed_test_menu
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
