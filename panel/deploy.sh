#!/bin/bash
#
# Monitoring Panel — установка: Docker, домен, SSL-сертификат, .env, контейнеры.
# Запускается установщиком (install.sh → «Установить панель») или напрямую
# из /opt/monitoring-panel. Поддерживаются Debian/Ubuntu.
#
# Необязательные переменные окружения:
#   DOMAIN        — домен панели (вопрос не задаётся)
#   CF_API_TOKEN  — API-токен Cloudflare: сертификат выпускается через DNS-01 без вопросов
#

set +e

# needrestart на Ubuntu 22.04+ открывает ncurses-диалог, который вешает скрипт
# и может перезапустить sshd, оборвав сессию
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
export NEEDRESTART_SUSPEND=1

LOCKFILE="/tmp/monitoring-panel-deploy.lock"
LOCK_FD=200

TIMEOUT_APT_UPDATE=120
TIMEOUT_APT_INSTALL=300
TIMEOUT_DOCKER_COMPOSE_DOWN=120
TIMEOUT_SYSTEMCTL=60
TIMEOUT_HEALTH_CHECK=5
TIMEOUT_CERTBOT=300
TIMEOUT_CF_VERIFY=15

MAX_RETRIES=3
RETRY_DELAY=5

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

CERT_RENEWAL_DAYS=30
LETSENCRYPT_LIVE="/etc/letsencrypt/live"
LETSENCRYPT_RENEWAL="/etc/letsencrypt/renewal"
# Токен Cloudflare хранится рядом с сертификатами: каталог смонтирован в контейнер
# бэкенда, поэтому продление из панели видит его по тому же пути
CLOUDFLARE_CREDENTIALS="/etc/letsencrypt/cloudflare.ini"
CLOUDFLARE_PROPAGATION_SECONDS=30
NGINX_RELOAD_CMD="docker exec panel-nginx nginx -s reload >/dev/null 2>&1 || true"

# existing — подходящий сертификат уже лежит в /etc/letsencrypt
# http      — certbot standalone (HTTP-01), нужен порт 80 и A-запись на этот сервер
# cloudflare — certbot + плагин dns-cloudflare (DNS-01), порт 80 не нужен
SSL_MODE=""

# ==================== Lock ====================

acquire_lock() {
    eval "exec $LOCK_FD>$LOCKFILE"
    if ! flock -n $LOCK_FD 2>/dev/null; then
        echo -e "\033[0;31m[✗]\033[0m $(msg lock_busy)"
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
    trap - EXIT INT TERM
    release_lock
    if [ $exit_code -ne 0 ] && [ $exit_code -ne 130 ] && [ $exit_code -ne 143 ]; then
        echo ""
        echo -e "\033[0;31m[✗]\033[0m $(msg failed_exit) $exit_code)"
    fi
    exit $exit_code
}

trap cleanup EXIT
trap 'echo ""; echo -e "\033[0;31m[✗]\033[0m $(msg interrupted)"; exit 130' INT
trap 'echo ""; echo -e "\033[0;31m[✗]\033[0m $(msg terminated)"; exit 143' TERM

# ==================== Colors ====================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_status() { echo -e "${GREEN}[✓]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[!]${NC} $1"; }
print_error() { echo -e "${RED}[✗]${NC} $1"; }
print_info() { echo -e "${CYAN}[i]${NC} $1"; }

# ==================== Translations ====================

# Язык выбирается один раз в install.sh и хранится в /etc/monitoring/language
LANG_CODE="en"
[ -f /etc/monitoring/language ] && LANG_CODE=$(cat /etc/monitoring/language 2>/dev/null || echo "en")

declare -A MSG_EN
declare -A MSG_RU

MSG_EN[title]="Monitoring Panel — installation"
MSG_EN[lock_busy]="Another panel deployment is already running"
MSG_EN[failed_exit]="Script failed (exit code"
MSG_EN[interrupted]="Interrupted by user (Ctrl+C)"
MSG_EN[terminated]="Terminated by signal"
MSG_EN[run_as_root]="Run as root: sudo ./deploy.sh"
MSG_EN[unsupported_os]="Unsupported OS — only Debian/Ubuntu are supported"
MSG_EN[apt_lock_wait]="Waiting for apt lock..."
MSG_EN[apt_lock_timeout]="apt lock wait timed out, trying anyway..."
MSG_EN[apt_update]="Updating package lists"
MSG_EN[apt_deps]="Installing dependencies"
MSG_EN[docker_installed]="Docker is installed"
MSG_EN[docker_installing]="Installing Docker..."
MSG_EN[docker_gpg]="Downloading Docker GPG key"
MSG_EN[docker_gpg_failed]="Failed to download Docker GPG key"
MSG_EN[docker_engine]="Installing Docker Engine"
MSG_EN[docker_failed]="Failed to install Docker"
MSG_EN[docker_done]="Docker installed"
MSG_EN[domain_title]="Panel domain"
MSG_EN[domain_hint]="The panel will be available at https://<domain>/<uid>."
MSG_EN[domain_prompt]="Domain (e.g. panel.example.com): "
MSG_EN[domain_required]="Domain is required"
MSG_EN[domain_invalid]="Invalid domain"
MSG_EN[domain_set]="Domain"
MSG_EN[too_many_attempts]="Too many invalid attempts"
MSG_EN[dns_checking]="Checking DNS for"
MSG_EN[dns_no_server_ip]="Could not detect this server's public IP — DNS check skipped"
MSG_EN[server_ip]="Server IP"
MSG_EN[domain_ip]="Domain resolves to"
MSG_EN[dns_not_resolving]="Domain does not resolve to any IP"
MSG_EN[dns_create_record]="Create an A-record"
MSG_EN[dns_ok]="DNS OK"
MSG_EN[dns_mismatch]="DNS mismatch: the domain points to another server"
MSG_EN[dns_fix_record]="Point the A-record to"
MSG_EN[dns_other_ip_note]="The domain points elsewhere (Cloudflare proxy?) — not a problem for DNS validation, continuing"
MSG_EN[dns_unresolved_note]="The domain does not resolve yet — the panel will be reachable once the A-record is created"
MSG_EN[continue_anyway]="Continue anyway? (y/N): "
MSG_EN[continuing_without_dns]="Continuing without DNS verification — HTTP validation may fail"
MSG_EN[cancelled_fix_dns]="Installation cancelled. Fix DNS and run again."
MSG_EN[fw_configuring]="Configuring firewall..."
MSG_EN[fw_ufw_ok]="UFW: ports 22, 80, 443 opened"
MSG_EN[fw_ufw_inactive]="UFW is inactive — rules added, firewall stays disabled"
MSG_EN[fw_iptables_ok]="iptables: ports 80, 443 opened"
MSG_EN[fw_none]="No firewall tool found — make sure ports 80 and 443 are open"
MSG_EN[port80_busy]="Port 80 is in use by another service"
MSG_EN[ssl_title]="SSL certificate"
MSG_EN[ssl_mode_prompt]="How should the certificate be obtained?"
MSG_EN[ssl_mode_http]="Let's Encrypt via HTTP — the domain must already point to this server, port 80 reachable"
MSG_EN[ssl_mode_cf]="Let's Encrypt via Cloudflare DNS API — works behind Cloudflare proxy, port 80 not needed"
MSG_EN[select_prompt]="Select [1-2]: "
MSG_EN[ssl_mode_selected_http]="Certificate: Let's Encrypt (HTTP validation)"
MSG_EN[ssl_mode_selected_cf]="Certificate: Let's Encrypt (Cloudflare DNS validation)"
MSG_EN[cf_token_hint]="Create a token at https://dash.cloudflare.com/profile/api-tokens using the «Edit zone DNS» template for the domain's zone."
MSG_EN[cf_token_prompt]="Cloudflare API token: "
MSG_EN[cf_token_required]="Token is required"
MSG_EN[cf_token_checking]="Verifying token..."
MSG_EN[cf_token_ok]="Token is valid"
MSG_EN[cf_token_invalid]="Cloudflare rejected the token"
MSG_EN[cf_token_unverified]="Could not reach the Cloudflare API to verify the token — continuing"
MSG_EN[cf_creds_saved]="Cloudflare credentials saved to"
MSG_EN[certbot_installed]="Certbot is installed"
MSG_EN[certbot_installing]="Installing Certbot"
MSG_EN[certbot_failed]="Failed to install Certbot"
MSG_EN[cert_obtaining]="Obtaining a Let's Encrypt certificate for"
MSG_EN[cert_port80_busy]="Port 80 is still in use. Stop that service and run again. Check with"
MSG_EN[cert_obtained]="Certificate obtained"
MSG_EN[cert_failed]="Failed to obtain the certificate"
MSG_EN[cert_http_hint1]="the domain points to this server's IP"
MSG_EN[cert_http_hint2]="port 80 is reachable from the internet"
MSG_EN[cert_http_hint3]="no other service is listening on port 80"
MSG_EN[cert_cf_hint1]="the token has «Zone → DNS → Edit» permission for the domain's zone"
MSG_EN[cert_cf_hint2]="the domain's DNS is hosted at Cloudflare"
MSG_EN[check_that]="Check that:"
MSG_EN[cert_renewing]="Renewing the certificate for"
MSG_EN[cert_renewed]="Certificate renewed"
MSG_EN[cert_renew_failed]="Failed to renew the certificate"
MSG_EN[cert_linked]="Using linked certificate"
MSG_EN[cert_linked_expired]="Linked certificate has expired — renew its source"
MSG_EN[cert_linked_unknown_expiry]="Cannot read the expiry of the linked certificate"
MSG_EN[valid_for]="valid for"
MSG_EN[days]="days"
MSG_EN[cert_expired]="Certificate has expired — renewing..."
MSG_EN[cert_unreadable]="Certificate exists but its expiry cannot be read — renewing..."
MSG_EN[cert_expiring_in]="Certificate expires in"
MSG_EN[cert_renew_now]="Renew now? (Y/n): "
MSG_EN[cert_renew_skipped]="Renewal skipped"
MSG_EN[cert_found_matching]="Found a matching certificate"
MSG_EN[cert_found_maybe_expired]="but it may be expired"
MSG_EN[cert_symlink_failed]="Failed to create symlink"
MSG_EN[cert_missing_after]="Certificate files not found after issuance"
MSG_EN[cert_ready]="SSL certificate ready"
MSG_EN[expires_in]="expires in"
MSG_EN[cron_external]="Certificate is linked — renewal is handled by its owner"
MSG_EN[cron_exists]="Auto-renewal is already scheduled"
MSG_EN[cron_setting]="Scheduling automatic renewal..."
MSG_EN[cron_failed]="Could not add the cron job"
MSG_EN[cron_added]="Auto-renewal scheduled: daily at 03:00"
MSG_EN[env_exists]=".env already exists — checking configuration..."
MSG_EN[env_domain_updated]="Domain updated in .env"
MSG_EN[env_pg_added]="PostgreSQL settings added to .env"
MSG_EN[env_enc_added]="PANEL_ENC_KEY added to .env"
MSG_EN[env_regen]="Regenerating credentials..."
MSG_EN[env_keep]="Using existing configuration"
MSG_EN[env_generating]="Generating .env..."
MSG_EN[env_generated]=".env generated"
MSG_EN[containers_stopping]="Stopping old containers"
MSG_EN[images_pulling]="Pulling Docker images"
MSG_EN[images_pull_failed]="Registry unavailable — building images locally..."
MSG_EN[images_base]="Pulling base images"
MSG_EN[images_building]="Building images from source"
MSG_EN[images_build_failed]="Failed to build images"
MSG_EN[containers_starting]="Starting containers"
MSG_EN[containers_failed]="Failed to start containers"
MSG_EN[health_waiting]="Waiting for the panel to come up..."
MSG_EN[health_ok]="Panel is up"
MSG_EN[health_timeout]="The panel did not respond in time — it may still be starting"
MSG_EN[commands]="Commands (run in /opt/monitoring-panel):"
MSG_EN[cmd_logs]="view logs"
MSG_EN[cmd_restart]="restart"
MSG_EN[cmd_down]="stop"
MSG_EN[cmd_certs]="certificate status"
MSG_EN[ssl_summary]="SSL certificate:"
MSG_EN[ssl_renewal_recommended]="renewal recommended"
MSG_EN[ssl_check_status]="check the status: certbot certificates"
MSG_EN[ssl_source]="Source"
MSG_EN[ssl_renew_external]="Renewal: handled by the source certificate's owner"
MSG_EN[ssl_renew_cron]="Renewal: automatic, daily at 03:00"
MSG_EN[creds_title]="PANEL LOGIN DETAILS"
MSG_EN[creds_url]="Panel URL:"
MSG_EN[creds_password]="Password:"
MSG_EN[creds_save]="Save these details. They are also stored in /opt/monitoring-panel/.env"

MSG_RU[title]="Установка панели мониторинга"
MSG_RU[lock_busy]="Установка панели уже запущена в другом процессе"
MSG_RU[failed_exit]="Скрипт завершился с ошибкой (код"
MSG_RU[interrupted]="Прервано пользователем (Ctrl+C)"
MSG_RU[terminated]="Остановлено сигналом"
MSG_RU[run_as_root]="Запустите от root: sudo ./deploy.sh"
MSG_RU[unsupported_os]="Неподдерживаемая ОС — поддерживаются только Debian/Ubuntu"
MSG_RU[apt_lock_wait]="Ожидание освобождения apt..."
MSG_RU[apt_lock_timeout]="apt так и не освободился, пробуем дальше..."
MSG_RU[apt_update]="Обновление списка пакетов"
MSG_RU[apt_deps]="Установка зависимостей"
MSG_RU[docker_installed]="Docker установлен"
MSG_RU[docker_installing]="Установка Docker..."
MSG_RU[docker_gpg]="Загрузка GPG-ключа Docker"
MSG_RU[docker_gpg_failed]="Не удалось загрузить GPG-ключ Docker"
MSG_RU[docker_engine]="Установка Docker Engine"
MSG_RU[docker_failed]="Не удалось установить Docker"
MSG_RU[docker_done]="Docker установлен"
MSG_RU[domain_title]="Домен панели"
MSG_RU[domain_hint]="Панель будет доступна по адресу https://<домен>/<uid>."
MSG_RU[domain_prompt]="Домен (например panel.example.com): "
MSG_RU[domain_required]="Домен обязателен"
MSG_RU[domain_invalid]="Некорректный домен"
MSG_RU[domain_set]="Домен"
MSG_RU[too_many_attempts]="Слишком много неверных попыток"
MSG_RU[dns_checking]="Проверка DNS для"
MSG_RU[dns_no_server_ip]="Не удалось определить публичный IP сервера — проверка DNS пропущена"
MSG_RU[server_ip]="IP сервера"
MSG_RU[domain_ip]="Домен указывает на"
MSG_RU[dns_not_resolving]="Домен не резолвится ни в один IP"
MSG_RU[dns_create_record]="Создайте A-запись"
MSG_RU[dns_ok]="DNS в порядке"
MSG_RU[dns_mismatch]="DNS не совпадает: домен указывает на другой сервер"
MSG_RU[dns_fix_record]="Направьте A-запись на"
MSG_RU[dns_other_ip_note]="Домен указывает на другой адрес (прокси Cloudflare?) — для DNS-проверки это не мешает, продолжаем"
MSG_RU[dns_unresolved_note]="Домен пока не резолвится — панель станет доступна после создания A-записи"
MSG_RU[continue_anyway]="Всё равно продолжить? (y/N): "
MSG_RU[continuing_without_dns]="Продолжаем без проверки DNS — HTTP-проверка может не пройти"
MSG_RU[cancelled_fix_dns]="Установка отменена. Исправьте DNS и запустите снова."
MSG_RU[fw_configuring]="Настройка файрвола..."
MSG_RU[fw_ufw_ok]="UFW: открыты порты 22, 80, 443"
MSG_RU[fw_ufw_inactive]="UFW не активен — правила добавлены, файрвол остаётся выключенным"
MSG_RU[fw_iptables_ok]="iptables: открыты порты 80, 443"
MSG_RU[fw_none]="Файрвол не найден — убедитесь, что порты 80 и 443 открыты"
MSG_RU[port80_busy]="Порт 80 занят другим сервисом"
MSG_RU[ssl_title]="SSL-сертификат"
MSG_RU[ssl_mode_prompt]="Как получить сертификат?"
MSG_RU[ssl_mode_http]="Let's Encrypt через HTTP — домен уже указывает на этот сервер, порт 80 доступен"
MSG_RU[ssl_mode_cf]="Let's Encrypt через Cloudflare DNS API — работает за прокси Cloudflare, порт 80 не нужен"
MSG_RU[select_prompt]="Выберите [1-2]: "
MSG_RU[ssl_mode_selected_http]="Сертификат: Let's Encrypt (проверка по HTTP)"
MSG_RU[ssl_mode_selected_cf]="Сертификат: Let's Encrypt (проверка через DNS Cloudflare)"
MSG_RU[cf_token_hint]="Создайте токен на https://dash.cloudflare.com/profile/api-tokens по шаблону «Edit zone DNS» для зоны домена."
MSG_RU[cf_token_prompt]="API-токен Cloudflare: "
MSG_RU[cf_token_required]="Токен обязателен"
MSG_RU[cf_token_checking]="Проверка токена..."
MSG_RU[cf_token_ok]="Токен действителен"
MSG_RU[cf_token_invalid]="Cloudflare отклонил токен"
MSG_RU[cf_token_unverified]="Не удалось проверить токен через API Cloudflare — продолжаем"
MSG_RU[cf_creds_saved]="Данные Cloudflare сохранены в"
MSG_RU[certbot_installed]="Certbot установлен"
MSG_RU[certbot_installing]="Установка Certbot"
MSG_RU[certbot_failed]="Не удалось установить Certbot"
MSG_RU[cert_obtaining]="Получение сертификата Let's Encrypt для"
MSG_RU[cert_port80_busy]="Порт 80 всё ещё занят. Остановите этот сервис и запустите снова. Проверить:"
MSG_RU[cert_obtained]="Сертификат получен"
MSG_RU[cert_failed]="Не удалось получить сертификат"
MSG_RU[cert_http_hint1]="домен указывает на IP этого сервера"
MSG_RU[cert_http_hint2]="порт 80 доступен из интернета"
MSG_RU[cert_http_hint3]="порт 80 не занят другим сервисом"
MSG_RU[cert_cf_hint1]="у токена есть право «Zone → DNS → Edit» на зону домена"
MSG_RU[cert_cf_hint2]="DNS домена обслуживается Cloudflare"
MSG_RU[check_that]="Проверьте, что:"
MSG_RU[cert_renewing]="Продление сертификата для"
MSG_RU[cert_renewed]="Сертификат продлён"
MSG_RU[cert_renew_failed]="Не удалось продлить сертификат"
MSG_RU[cert_linked]="Используется связанный сертификат"
MSG_RU[cert_linked_expired]="Связанный сертификат истёк — продлите его источник"
MSG_RU[cert_linked_unknown_expiry]="Не удалось прочитать срок связанного сертификата"
MSG_RU[valid_for]="действителен ещё"
MSG_RU[days]="дн."
MSG_RU[cert_expired]="Сертификат истёк — продление..."
MSG_RU[cert_unreadable]="Сертификат есть, но срок не читается — продление..."
MSG_RU[cert_expiring_in]="Сертификат истекает через"
MSG_RU[cert_renew_now]="Продлить сейчас? (Y/n): "
MSG_RU[cert_renew_skipped]="Продление пропущено"
MSG_RU[cert_found_matching]="Найден подходящий сертификат"
MSG_RU[cert_found_maybe_expired]="но он, возможно, истёк"
MSG_RU[cert_symlink_failed]="Не удалось создать symlink"
MSG_RU[cert_missing_after]="Файлы сертификата не найдены после выпуска"
MSG_RU[cert_ready]="SSL-сертификат готов"
MSG_RU[expires_in]="истекает через"
MSG_RU[cron_external]="Сертификат связанный — продление на стороне его владельца"
MSG_RU[cron_exists]="Автопродление уже настроено"
MSG_RU[cron_setting]="Настройка автопродления..."
MSG_RU[cron_failed]="Не удалось добавить задачу cron"
MSG_RU[cron_added]="Автопродление настроено: ежедневно в 03:00"
MSG_RU[env_exists]=".env уже есть — проверка конфигурации..."
MSG_RU[env_domain_updated]="Домен обновлён в .env"
MSG_RU[env_pg_added]="Настройки PostgreSQL добавлены в .env"
MSG_RU[env_enc_added]="PANEL_ENC_KEY добавлен в .env"
MSG_RU[env_regen]="Пересоздание учётных данных..."
MSG_RU[env_keep]="Используется существующая конфигурация"
MSG_RU[env_generating]="Генерация .env..."
MSG_RU[env_generated]=".env создан"
MSG_RU[containers_stopping]="Остановка старых контейнеров"
MSG_RU[images_pulling]="Загрузка Docker-образов"
MSG_RU[images_pull_failed]="Реестр недоступен — сборка образов локально..."
MSG_RU[images_base]="Загрузка базовых образов"
MSG_RU[images_building]="Сборка образов из исходников"
MSG_RU[images_build_failed]="Не удалось собрать образы"
MSG_RU[containers_starting]="Запуск контейнеров"
MSG_RU[containers_failed]="Не удалось запустить контейнеры"
MSG_RU[health_waiting]="Ожидание запуска панели..."
MSG_RU[health_ok]="Панель запущена"
MSG_RU[health_timeout]="Панель не ответила вовремя — возможно, ещё запускается"
MSG_RU[commands]="Команды (из каталога /opt/monitoring-panel):"
MSG_RU[cmd_logs]="логи"
MSG_RU[cmd_restart]="перезапуск"
MSG_RU[cmd_down]="остановка"
MSG_RU[cmd_certs]="статус сертификата"
MSG_RU[ssl_summary]="SSL-сертификат:"
MSG_RU[ssl_renewal_recommended]="рекомендуется продлить"
MSG_RU[ssl_check_status]="проверьте состояние: certbot certificates"
MSG_RU[ssl_source]="Источник"
MSG_RU[ssl_renew_external]="Продление: на стороне владельца исходного сертификата"
MSG_RU[ssl_renew_cron]="Продление: автоматически, ежедневно в 03:00"
MSG_RU[creds_title]="ДАННЫЕ ДЛЯ ВХОДА В ПАНЕЛЬ"
MSG_RU[creds_url]="Адрес панели:"
MSG_RU[creds_password]="Пароль:"
MSG_RU[creds_save]="Сохраните эти данные. Они также лежат в /opt/monitoring-panel/.env"

msg() {
    local key="$1"
    if [ "$LANG_CODE" = "ru" ]; then
        echo "${MSG_RU[$key]:-${MSG_EN[$key]:-$key}}"
    else
        echo "${MSG_EN[$key]:-$key}"
    fi
}

# ==================== Input & progress helpers ====================

# Чтение с таймаутом и значением по умолчанию; вне TTY сразу возвращает default
safe_read() {
    local prompt="$1" default="$2" timeout="${3:-30}" input=""
    if [ ! -t 0 ]; then
        echo "$default"
        return
    fi
    printf "%s" "$prompt" >/dev/tty 2>/dev/null || printf "%s" "$prompt"
    if read -t "$timeout" -r input </dev/tty 2>/dev/null; then
        [ -n "$input" ] && echo "$input" || echo "$default"
    else
        # Таймаут: курсор остался на строке подсказки
        echo "" >/dev/tty 2>/dev/null || true
        echo "$default"
    fi
}

# То же без эха — для токенов
safe_read_secret() {
    local prompt="$1" timeout="${2:-300}" input=""
    [ -t 0 ] || { echo ""; return; }
    printf "%s" "$prompt" >/dev/tty 2>/dev/null
    read -t "$timeout" -r -s input </dev/tty 2>/dev/null
    echo "" >/dev/tty 2>/dev/null
    echo "$input"
}

# Запуск команды со спиннером и временем выполнения
spin() {
    local desc="$1"; shift
    local logf
    logf=$(mktemp /tmp/.spin-XXXXXX 2>/dev/null || echo "/tmp/.spin-$$")
    local chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local t0
    t0=$(date +%s)

    "$@" >"$logf" 2>&1 &
    local pid=$!

    # Анимация только в реальном терминале: вне TTY (запуск по SSH из панели)
    # \r-перерисовка превращается в мусор
    if [ -t 1 ]; then
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
    else
        echo "  • ${desc}..."
    fi

    wait "$pid" 2>/dev/null
    local rc=$?
    local e=$(( $(date +%s) - t0 ))
    [ -t 1 ] && printf "\r\033[2K"

    if [ $rc -eq 0 ]; then
        echo -e "  ${GREEN}✓${NC} ${desc} ${CYAN}(${e}s)${NC}"
    else
        echo -e "  ${RED}✗${NC} ${desc} ${RED}(${e}s)${NC}"
        if [ -s "$logf" ]; then
            echo -e "    ${RED}┌──────────────────────────────────────────${NC}"
            tail -15 "$logf" | while IFS= read -r line; do
                echo -e "    ${RED}│${NC} $line"
            done
            echo -e "    ${RED}└──────────────────────────────────────────${NC}"
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

# ==================== Proxy ====================

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
    timeout "$TIMEOUT_SYSTEMCTL" systemctl daemon-reload >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl restart docker >/dev/null 2>&1 || true
}

# ==================== APT ====================

suppress_needrestart() {
    if [ -d /etc/needrestart ] || dpkg -l needrestart &>/dev/null 2>&1; then
        mkdir -p /etc/needrestart/conf.d 2>/dev/null || true
        echo '$nrconf{restart} = "l";' > /etc/needrestart/conf.d/no-prompt.conf 2>/dev/null || true
    fi
    pkill -9 needrestart 2>/dev/null || true
}

wait_for_apt_lock() {
    local max_wait=120
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
        [ $waited -eq 0 ] && print_warning "$(msg apt_lock_wait)"
        sleep 3
        waited=$((waited + 3))
        if [ $waited -ge $max_wait ]; then
            print_warning "$(msg apt_lock_timeout)"
            return 0
        fi
    done
    return 0
}

apt_update() {
    suppress_needrestart
    wait_for_apt_lock
    spin_retry "$TIMEOUT_APT_UPDATE" "$MAX_RETRIES" "$RETRY_DELAY" "$(msg apt_update)" \
        env DEBIAN_FRONTEND=noninteractive apt-get update -qq
}

# apt_install "<описание>" пакет...
apt_install() {
    local desc="$1"; shift
    suppress_needrestart
    wait_for_apt_lock
    spin_retry "$TIMEOUT_APT_INSTALL" "$MAX_RETRIES" "$RETRY_DELAY" "$desc" \
        env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l NEEDRESTART_SUSPEND=1 \
        apt-get install -y -qq \
        -o Dpkg::Options::="--force-confold" \
        -o Dpkg::Options::="--force-confdef" \
        "$@"
}

# ==================== Docker ====================

check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "$(msg run_as_root)"
        exit 1
    fi
}

check_os() {
    [ -f /etc/debian_version ] && return 0
    print_error "$(msg unsupported_os)"
    exit 1
}

install_docker() {
    print_info "$(msg docker_installing)"

    local os_id os_codename
    os_id=$(. /etc/os-release && echo "$ID")
    os_codename=$(. /etc/os-release && echo "$VERSION_CODENAME")

    apt_update || true
    apt_install "$(msg apt_deps)" ca-certificates curl gnupg || {
        print_error "$(msg docker_failed)"
        return 1
    }

    install -m 0755 -d /etc/apt/keyrings 2>/dev/null || true
    rm -f /etc/apt/keyrings/docker.gpg
    if ! spin "$(msg docker_gpg)" bash -c \
        "curl -fsSL 'https://download.docker.com/linux/${os_id}/gpg' | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg 2>/dev/null"; then
        print_error "$(msg docker_gpg_failed)"
        return 1
    fi
    chmod a+r /etc/apt/keyrings/docker.gpg 2>/dev/null || true

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${os_id} ${os_codename} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt_update || true
    apt_install "$(msg docker_engine)" docker-ce docker-ce-cli containerd.io docker-compose-plugin || {
        print_error "$(msg docker_failed)"
        return 1
    }

    timeout "$TIMEOUT_SYSTEMCTL" systemctl enable --now docker >/dev/null 2>&1 || true
    print_status "$(msg docker_done)"
}

ensure_docker() {
    if command -v docker &>/dev/null; then
        print_status "$(msg docker_installed)"
        return 0
    fi
    install_docker
}

# ==================== Domain & DNS ====================

generate_random() {
    local length=$1
    openssl rand -hex $((length / 2)) 2>/dev/null || tr -dc 'a-zA-Z0-9' </dev/urandom | head -c "$length"
}

prompt_domain() {
    [ -n "$DOMAIN" ] && return 0

    echo ""
    echo -e "${YELLOW}══ $(msg domain_title) ══${NC}"
    echo ""
    echo -e "$(msg domain_hint)"
    echo ""

    local attempt=0
    while [ $attempt -lt 5 ]; do
        DOMAIN=$(safe_read "$(msg domain_prompt)" "" 300)

        if [ -z "$DOMAIN" ]; then
            print_error "$(msg domain_required)"
            attempt=$((attempt + 1))
            continue
        fi

        if echo "$DOMAIN" | grep -qE '^([a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$'; then
            print_status "$(msg domain_set): ${DOMAIN}"
            return 0
        fi

        print_error "$(msg domain_invalid): ${DOMAIN}"
        attempt=$((attempt + 1))
    done

    print_error "$(msg too_many_attempts)"
    exit 1
}

get_server_ip() {
    local ip=""
    local services=(
        "https://api.ipify.org"
        "https://icanhazip.com"
        "https://ifconfig.me"
        "https://checkip.amazonaws.com"
        "https://ipinfo.io/ip"
        "https://ident.me"
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

    hostname -I 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

resolve_domain_ip() {
    local domain="$1" ip=""

    if command -v dig &>/dev/null; then
        ip=$(dig +short "$domain" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    fi
    if [ -z "$ip" ] && command -v getent &>/dev/null; then
        ip=$(getent ahostsv4 "$domain" 2>/dev/null | awk '/STREAM/ {print $1}' | head -1)
    fi

    echo "$ip"
}

# Для HTTP-проверки DNS должен вести на этот сервер — иначе certbot гарантированно
# упадёт, поэтому спрашиваем подтверждение. Для DNS-01 и уже существующего
# сертификата расхождение не мешает выпуску (домен может быть за прокси Cloudflare).
verify_domain_dns() {
    print_info "$(msg dns_checking) ${DOMAIN}..."

    local server_ip domain_ip
    server_ip=$(get_server_ip)
    if [ -z "$server_ip" ]; then
        print_warning "$(msg dns_no_server_ip)"
        return 0
    fi
    print_info "$(msg server_ip): ${server_ip}"

    domain_ip=$(resolve_domain_ip "$DOMAIN")

    if [ -z "$domain_ip" ]; then
        if [ "$SSL_MODE" != "http" ]; then
            print_warning "$(msg dns_unresolved_note)"
            print_info "$(msg dns_create_record): ${DOMAIN} → ${server_ip}"
            return 0
        fi
        echo ""
        print_error "$(msg dns_not_resolving): ${DOMAIN}"
        print_info "$(msg dns_create_record): ${DOMAIN} → ${server_ip}"
        echo ""
        confirm_continue_without_dns
        return $?
    fi

    print_info "$(msg domain_ip): ${domain_ip}"

    if [ "$domain_ip" = "$server_ip" ]; then
        print_status "$(msg dns_ok): ${DOMAIN} → ${server_ip}"
        return 0
    fi

    if [ "$SSL_MODE" != "http" ]; then
        print_warning "$(msg dns_other_ip_note)"
        return 0
    fi

    echo ""
    print_error "$(msg dns_mismatch)"
    echo -e "    ${DOMAIN} → ${RED}${domain_ip}${NC}"
    echo -e "    $(msg server_ip): ${GREEN}${server_ip}${NC}"
    print_info "$(msg dns_fix_record): ${DOMAIN} → ${server_ip}"
    echo ""
    confirm_continue_without_dns
}

confirm_continue_without_dns() {
    local choice
    choice=$(safe_read "$(msg continue_anyway)" "n" 60)
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
        print_warning "$(msg continuing_without_dns)"
        return 0
    fi
    print_info "$(msg cancelled_fix_dns)"
    return 1
}

# ==================== Firewall ====================

setup_firewall() {
    print_info "$(msg fw_configuring)"

    if command -v ufw &>/dev/null; then
        ufw allow 22/tcp >/dev/null 2>&1 || true
        ufw allow 80/tcp >/dev/null 2>&1 || true
        ufw allow 443/tcp >/dev/null 2>&1 || true
        if ufw status 2>/dev/null | grep -q "Status: active"; then
            print_status "$(msg fw_ufw_ok)"
        else
            print_warning "$(msg fw_ufw_inactive)"
        fi
    elif command -v iptables &>/dev/null; then
        iptables -I INPUT -p tcp --dport 80 -j ACCEPT >/dev/null 2>&1 || true
        iptables -I INPUT -p tcp --dport 443 -j ACCEPT >/dev/null 2>&1 || true
        if command -v netfilter-persistent &>/dev/null; then
            netfilter-persistent save >/dev/null 2>&1 || true
        elif [ -d /etc/iptables ]; then
            iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
        fi
        print_status "$(msg fw_iptables_ok)"
    else
        print_warning "$(msg fw_none)"
    fi

    if ss -tuln 2>/dev/null | grep -q ':80 '; then
        print_warning "$(msg port80_busy)"
    fi
}

# ==================== SSL: mode selection ====================

panel_cert_path() { echo "${LETSENCRYPT_LIVE}/${DOMAIN}"; }

panel_cert_present() {
    local cert_path
    cert_path=$(panel_cert_path)
    [ -f "${cert_path}/fullchain.pem" ] && [ -f "${cert_path}/privkey.pem" ]
}

# Способ выпуска берётся из renewal-конфига certbot: standalone | dns-cloudflare
cert_authenticator() {
    sed -n 's/^authenticator *= *//p' "${LETSENCRYPT_RENEWAL}/${DOMAIN}.conf" 2>/dev/null | head -1
}

# Ищет в /etc/letsencrypt/live сертификат, покрывающий домен по SAN — в том числе
# wildcard на один уровень (*.example.com покрывает panel.example.com)
find_existing_cert() {
    local domain="$1"
    [ -d "$LETSENCRYPT_LIVE" ] || return 1

    for cert_dir in "${LETSENCRYPT_LIVE}"/*/; do
        [ -f "${cert_dir}fullchain.pem" ] && [ -f "${cert_dir}privkey.pem" ] || continue

        local sans
        sans=$(openssl x509 -in "${cert_dir}fullchain.pem" -noout -ext subjectAltName 2>/dev/null \
            | grep -oE 'DNS:[^ ,]+' | sed 's/DNS://g')

        for san in $sans; do
            [ "$san" = "$domain" ] && { echo "${cert_dir%/}"; return 0; }
            if [[ "$san" == \*.* ]] && [[ "$domain" == *.* ]] && [ "${domain#*.}" = "${san#\*.}" ]; then
                echo "${cert_dir%/}"
                return 0
            fi
        done
    done

    return 1
}

resolve_ssl_mode() {
    if panel_cert_present || find_existing_cert "$DOMAIN" >/dev/null; then
        SSL_MODE="existing"
        return 0
    fi

    if [ -n "${CF_API_TOKEN:-}" ]; then
        SSL_MODE="cloudflare"
        print_status "$(msg ssl_mode_selected_cf)"
        save_cloudflare_credentials "$CF_API_TOKEN"
        return 0
    fi

    echo ""
    echo -e "${YELLOW}══ $(msg ssl_title) ══${NC}"
    echo ""
    echo -e "$(msg ssl_mode_prompt)"
    echo -e "  ${GREEN}1)${NC} $(msg ssl_mode_http)"
    echo -e "  ${GREEN}2)${NC} $(msg ssl_mode_cf)"
    echo ""

    local choice
    choice=$(safe_read "$(msg select_prompt)" "1" 300)
    if [ "$choice" != "2" ]; then
        SSL_MODE="http"
        print_status "$(msg ssl_mode_selected_http)"
        return 0
    fi

    SSL_MODE="cloudflare"
    print_status "$(msg ssl_mode_selected_cf)"
    prompt_cloudflare_token || exit 1
}

# 0 — токен активен, 1 — Cloudflare его отклонил, 2 — API недоступен
verify_cloudflare_token() {
    local token="$1" response
    response=$(timeout "$TIMEOUT_CF_VERIFY" curl -sS --max-time 10 \
        -H "Authorization: Bearer ${token}" \
        "https://api.cloudflare.com/client/v4/user/tokens/verify" 2>/dev/null) || return 2
    [ -n "$response" ] || return 2
    echo "$response" | grep -q '"status":"active"' && return 0
    return 1
}

save_cloudflare_credentials() {
    local token="$1"
    mkdir -p "$(dirname "$CLOUDFLARE_CREDENTIALS")" 2>/dev/null || true
    printf 'dns_cloudflare_api_token = %s\n' "$token" > "$CLOUDFLARE_CREDENTIALS"
    chmod 600 "$CLOUDFLARE_CREDENTIALS"
    print_status "$(msg cf_creds_saved) ${CLOUDFLARE_CREDENTIALS}"
}

prompt_cloudflare_token() {
    echo ""
    echo -e "$(msg cf_token_hint)"
    echo ""

    local attempt=0 token
    while [ $attempt -lt 3 ]; do
        token=$(safe_read_secret "$(msg cf_token_prompt)" 300)
        if [ -z "$token" ]; then
            print_error "$(msg cf_token_required)"
            attempt=$((attempt + 1))
            continue
        fi

        print_info "$(msg cf_token_checking)"
        verify_cloudflare_token "$token"
        case $? in
            0) print_status "$(msg cf_token_ok)" ;;
            1) print_error "$(msg cf_token_invalid)"; attempt=$((attempt + 1)); continue ;;
            *) print_warning "$(msg cf_token_unverified)" ;;
        esac

        save_cloudflare_credentials "$token"
        return 0
    done

    print_error "$(msg too_many_attempts)"
    return 1
}

# ==================== SSL: certbot ====================

# install_certbot [cloudflare] — с плагином DNS-01 при необходимости
install_certbot() {
    local packages=(certbot)
    local missing=0
    command -v certbot &>/dev/null || missing=1

    if [ "${1:-}" = "cloudflare" ]; then
        packages+=(python3-certbot-dns-cloudflare)
        dpkg -s python3-certbot-dns-cloudflare >/dev/null 2>&1 || missing=1
    fi

    if [ $missing -eq 0 ]; then
        print_status "$(msg certbot_installed)"
        return 0
    fi

    apt_update || true
    apt_install "$(msg certbot_installing)" "${packages[@]}" || {
        print_error "$(msg certbot_failed)"
        return 1
    }
    print_status "$(msg certbot_installed)"
}

get_cert_days_remaining() {
    local cert_file
    cert_file="$(panel_cert_path)/fullchain.pem"
    [ -f "$cert_file" ] || { echo "-1"; return; }

    local expiry_date expiry_epoch
    expiry_date=$(openssl x509 -enddate -noout -in "$cert_file" 2>/dev/null | cut -d= -f2)
    expiry_epoch=$(date -d "$expiry_date" +%s 2>/dev/null)
    [ -n "$expiry_epoch" ] || { echo "-1"; return; }

    echo $(( (expiry_epoch - $(date +%s)) / 86400 ))
}

stop_port_80_services() {
    timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl stop nginx >/dev/null 2>&1 || true
    timeout "$TIMEOUT_SYSTEMCTL" systemctl stop apache2 >/dev/null 2>&1 || true
    sleep 2
}

print_cert_failure_hints() {
    print_info "$(msg check_that)"
    if [ "$SSL_MODE" = "cloudflare" ]; then
        echo "  1. $(msg cert_cf_hint1)"
        echo "  2. $(msg cert_cf_hint2)"
    else
        echo "  1. $(msg cert_http_hint1)"
        echo "  2. $(msg cert_http_hint2)"
        echo "  3. $(msg cert_http_hint3)"
    fi
}

obtain_certificate_http() {
    stop_port_80_services
    if ss -tuln 2>/dev/null | grep -q ':80 '; then
        print_error "$(msg cert_port80_busy) ss -tuln | grep :80"
        return 1
    fi

    timeout "$TIMEOUT_CERTBOT" certbot certonly --standalone --non-interactive --agree-tos \
        --register-unsafely-without-email \
        -d "$DOMAIN" 2>&1
}

obtain_certificate_cloudflare() {
    timeout "$TIMEOUT_CERTBOT" certbot certonly --non-interactive --agree-tos \
        --register-unsafely-without-email \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CLOUDFLARE_CREDENTIALS" \
        --dns-cloudflare-propagation-seconds "$CLOUDFLARE_PROPAGATION_SECONDS" \
        -d "$DOMAIN" 2>&1
}

obtain_certificate() {
    print_info "$(msg cert_obtaining) ${DOMAIN}..."

    local ok=1
    if [ "$SSL_MODE" = "cloudflare" ]; then
        obtain_certificate_cloudflare && ok=0
    else
        obtain_certificate_http && ok=0
    fi

    if [ $ok -eq 0 ]; then
        print_status "$(msg cert_obtained)"
        return 0
    fi

    print_error "$(msg cert_failed)"
    print_cert_failure_hints
    return 1
}

# Продление собственной линии панели тем же способом, которым она выпущена
renew_certificate() {
    print_info "$(msg cert_renewing) ${DOMAIN}..."

    if [ "$(cert_authenticator)" = "dns-cloudflare" ]; then
        install_certbot cloudflare || return 1
        timeout "$TIMEOUT_CERTBOT" certbot renew --cert-name "$DOMAIN" --non-interactive 2>&1
    else
        install_certbot || return 1
        stop_port_80_services
        timeout "$TIMEOUT_CERTBOT" certbot renew --cert-name "$DOMAIN" --standalone --non-interactive 2>&1
    fi

    if [ $? -eq 0 ]; then
        print_status "$(msg cert_renewed)"
        return 0
    fi
    print_error "$(msg cert_renew_failed)"
    return 1
}

report_linked_certificate() {
    local cert_path real_path days_remaining
    cert_path=$(panel_cert_path)
    real_path=$(readlink -f "$cert_path" 2>/dev/null || readlink "$cert_path")
    days_remaining=$(get_cert_days_remaining)

    if [ "$days_remaining" -gt 0 ]; then
        print_status "$(msg cert_linked): $(basename "$real_path") ($(msg valid_for) ${days_remaining} $(msg days))"
        return 0
    fi
    if [ "$days_remaining" -eq -1 ]; then
        print_warning "$(msg cert_linked_unknown_expiry): ${real_path}"
        return 0
    fi
    print_error "$(msg cert_linked_expired): ${real_path}"
    return 1
}

# Сертификат панели уже лежит по прямому пути: symlink не трогаем, свою линию
# при необходимости продлеваем
ensure_direct_certificate_fresh() {
    local cert_path
    cert_path=$(panel_cert_path)
    [ -L "$cert_path" ] && { report_linked_certificate; return $?; }

    local days_remaining
    days_remaining=$(get_cert_days_remaining)

    if [ "$days_remaining" -lt 0 ]; then
        print_warning "$(msg cert_unreadable)"
        renew_certificate || return 1
    elif [ "$days_remaining" -le 0 ]; then
        print_error "$(msg cert_expired)"
        renew_certificate || return 1
    elif [ "$days_remaining" -le "$CERT_RENEWAL_DAYS" ]; then
        print_warning "$(msg cert_expiring_in) ${days_remaining} $(msg days)"
        local renew_choice
        renew_choice=$(safe_read "$(msg cert_renew_now)" "Y" 60)
        if [ "$renew_choice" != "n" ] && [ "$renew_choice" != "N" ]; then
            renew_certificate || return 1
        else
            print_info "$(msg cert_renew_skipped)"
        fi
    fi

    days_remaining=$(get_cert_days_remaining)
    [ "$days_remaining" -gt 0 ] && print_status "$(msg cert_ready) ($(msg expires_in) ${days_remaining} $(msg days))"
    return 0
}

link_matching_certificate() {
    local found_cert="$1" cert_path
    cert_path=$(panel_cert_path)

    # Пустой каталог помешал бы symlink
    if [ -d "$cert_path" ] && [ ! -L "$cert_path" ]; then
        rmdir "$cert_path" 2>/dev/null || true
    fi
    if [ ! -e "$cert_path" ]; then
        ln -s "$found_cert" "$cert_path" 2>/dev/null || {
            print_error "$(msg cert_symlink_failed): ${cert_path} → ${found_cert}"
            return 1
        }
    fi

    local days_remaining
    days_remaining=$(get_cert_days_remaining)
    if [ "$days_remaining" -gt 0 ]; then
        print_status "$(msg cert_found_matching): $(basename "$found_cert") ($(msg valid_for) ${days_remaining} $(msg days))"
    else
        print_warning "$(msg cert_found_matching): $(basename "$found_cert"), $(msg cert_found_maybe_expired)"
    fi
}

setup_ssl_certificate() {
    if panel_cert_present; then
        ensure_direct_certificate_fresh
        return $?
    fi

    local found_cert
    if found_cert=$(find_existing_cert "$DOMAIN"); then
        link_matching_certificate "$found_cert"
        return $?
    fi

    if [ "$SSL_MODE" = "cloudflare" ]; then
        install_certbot cloudflare || return 1
    else
        install_certbot || return 1
    fi
    obtain_certificate || return 1

    if ! panel_cert_present; then
        print_error "$(msg cert_missing_after)"
        return 1
    fi

    local days_remaining
    days_remaining=$(get_cert_days_remaining)
    [ "$days_remaining" -gt 0 ] && print_status "$(msg cert_ready) ($(msg expires_in) ${days_remaining} $(msg days))"
    return 0
}

# Ежедневная задача certbot только для собственной линии панели (--cert-name):
# wildcard-сертификаты продлевает панель через Cloudflare, их не трогаем.
# standalone: panel-nginx держит порт 80, поэтому контейнер останавливается на время
# проверки (--pre-hook) и поднимается обратно (--post-hook).
# dns-cloudflare: порт не нужен, после продления nginx просто перечитывает файлы.
setup_cert_renewal_cron() {
    if [ -L "$(panel_cert_path)" ]; then
        print_info "$(msg cron_external)"
        if crontab -l 2>/dev/null | grep -q "certbot renew"; then
            crontab -l 2>/dev/null | grep -v "certbot renew" | crontab - 2>/dev/null || true
        fi
        return 0
    fi

    local cron_job
    if [ "$(cert_authenticator)" = "dns-cloudflare" ]; then
        cron_job="0 3 * * * certbot renew --cert-name '${DOMAIN}' --quiet --deploy-hook '${NGINX_RELOAD_CMD}'"
    else
        cron_job="0 3 * * * certbot renew --cert-name '${DOMAIN}' --quiet --pre-hook 'docker stop panel-nginx >/dev/null 2>&1 || true' --post-hook 'docker start panel-nginx >/dev/null 2>&1 || true'"
    fi

    if crontab -l 2>/dev/null | grep -qF "$cron_job"; then
        print_status "$(msg cron_exists)"
        return 0
    fi

    print_info "$(msg cron_setting)"
    # Прежняя задача certbot renew (в т.ч. с другими хуками) заменяется актуальной
    { crontab -l 2>/dev/null | grep -v "certbot renew"; echo "$cron_job"; } | crontab - 2>/dev/null || {
        print_warning "$(msg cron_failed)"
        return 1
    }
    print_status "$(msg cron_added)"
}

# ==================== .env ====================

# Расчёт настроек PostgreSQL живёт в общем scripts/pg-tune.sh — тот же файл
# подключает апдейтер, чтобы формулы на свежей установке и после обновления
# не разъезжались
if [ -f "$SCRIPT_DIR/scripts/pg-tune.sh" ]; then
    . "$SCRIPT_DIR/scripts/pg-tune.sh"
else
    tune_postgres_env() { :; }
fi

generate_env() {
    if [ -f .env ]; then
        print_warning "$(msg env_exists)"
        source .env 2>/dev/null || true

        if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "$(grep '^DOMAIN=' .env 2>/dev/null | cut -d= -f2)" ]; then
            sed -i "s/^DOMAIN=.*/DOMAIN=${DOMAIN}/" .env
            print_info "$(msg env_domain_updated)"
        fi

        if ! grep -q "^POSTGRES_PASSWORD=" .env 2>/dev/null; then
            cat >> .env << EOF

# PostgreSQL (auto-generated)
POSTGRES_USER=panel
POSTGRES_PASSWORD=$(generate_random 32)
POSTGRES_DB=panel
EOF
            print_status "$(msg env_pg_added)"
        fi

        if ! grep -q "^PANEL_ENC_KEY=" .env 2>/dev/null; then
            printf 'PANEL_ENC_KEY=%s\n' "$(openssl rand -base64 32 | tr -d '\n')" >> .env
            print_status "$(msg env_enc_added)"
        fi

        if [ -n "$PANEL_UID" ] && [ "$PANEL_UID" != "changeme" ]; then
            print_status "$(msg env_keep)"
            return
        fi
        print_info "$(msg env_regen)"
    fi

    print_info "$(msg env_generating)"

    cat > .env << EOF
# Panel domain (used for SSL and nginx)
DOMAIN=${DOMAIN}

# Panel login (auto-generated): https://${DOMAIN}/<PANEL_UID>
PANEL_UID=$(generate_random 16)
PANEL_PASSWORD=$(generate_random 32)

# JWT
JWT_SECRET=$(generate_random 64)
JWT_EXPIRE_MINUTES=1440

# Secret encryption key (base64, 32 bytes) — back it up together with the database!
PANEL_ENC_KEY=$(openssl rand -base64 32 | tr -d '\n')

# Login protection
MAX_FAILED_ATTEMPTS=5
BAN_DURATION_SECONDS=900

# PostgreSQL (auto-generated)
POSTGRES_USER=panel
POSTGRES_PASSWORD=$(generate_random 32)
POSTGRES_DB=panel

# Ports
PANEL_PORT=443
PANEL_HTTP_PORT=80
EOF

    chmod 600 .env 2>/dev/null || true
    print_status "$(msg env_generated)"
}

# ==================== Containers ====================

pull_and_start() {
    spin "$(msg containers_stopping)" \
        timeout "$TIMEOUT_DOCKER_COMPOSE_DOWN" docker compose down --remove-orphans 2>/dev/null || true

    if ! spin_retry 240 5 10 "$(msg images_pulling)" docker compose pull 2>/dev/null; then
        print_warning "$(msg images_pull_failed)"
        spin "$(msg images_base)" bash -c 'docker compose pull --ignore-buildable 2>/dev/null || true'
        spin_retry 600 2 10 "$(msg images_building)" docker compose build || {
            print_error "$(msg images_build_failed)"
            exit 1
        }
    fi

    spin "$(msg containers_starting)" docker compose up -d || {
        print_error "$(msg containers_failed)"
        exit 1
    }
}

wait_for_health() {
    print_info "$(msg health_waiting)"

    local attempt=0
    while [ $attempt -lt 30 ]; do
        if timeout "$TIMEOUT_HEALTH_CHECK" curl -sk "https://localhost/health" >/dev/null 2>&1 || \
           timeout "$TIMEOUT_HEALTH_CHECK" curl -sk "https://${DOMAIN}/health" >/dev/null 2>&1; then
            print_status "$(msg health_ok)"
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 2
    done

    print_warning "$(msg health_timeout)"
}

print_credentials() {
    source .env 2>/dev/null || true

    local cert_path days_remaining
    cert_path=$(panel_cert_path)
    days_remaining=$(get_cert_days_remaining)

    echo ""
    echo -e "${CYAN}$(msg commands)${NC}"
    echo "  docker compose logs -f     # $(msg cmd_logs)"
    echo "  docker compose restart     # $(msg cmd_restart)"
    echo "  docker compose down        # $(msg cmd_down)"
    echo "  certbot certificates       # $(msg cmd_certs)"
    echo ""

    echo -e "${CYAN}$(msg ssl_summary)${NC}"
    if [ "$days_remaining" -gt "$CERT_RENEWAL_DAYS" ]; then
        echo -e "  ${GREEN}$(msg valid_for) ${days_remaining} $(msg days)${NC}"
    elif [ "$days_remaining" -gt 0 ]; then
        echo -e "  ${YELLOW}$(msg expires_in) ${days_remaining} $(msg days) ($(msg ssl_renewal_recommended))${NC}"
    else
        echo -e "  ${RED}$(msg ssl_check_status)${NC}"
    fi
    if [ -L "$cert_path" ]; then
        echo -e "  $(msg ssl_source): ${CYAN}$(readlink -f "$cert_path" 2>/dev/null || readlink "$cert_path")${NC}"
        echo -e "  $(msg ssl_renew_external)"
    else
        echo -e "  $(msg ssl_renew_cron)"
    fi

    echo ""
    echo -e "  ${GREEN}══ $(msg creds_title) ══${NC}"
    echo ""
    echo -e "    ${YELLOW}$(msg creds_url)${NC}"
    echo -e "    ${CYAN}https://${DOMAIN}/${PANEL_UID}${NC}"
    echo ""
    echo -e "    ${YELLOW}$(msg creds_password)${NC}"
    echo -e "    ${CYAN}${PANEL_PASSWORD}${NC}"
    echo ""
    echo -e "  ${RED}$(msg creds_save)${NC}"
    echo ""
}

# ==================== Main ====================

main() {
    acquire_lock
    check_root
    check_os
    load_proxy
    configure_apt_proxy

    echo ""
    echo -e "${CYAN}══ $(msg title) ══${NC}"
    echo ""

    ensure_docker || exit 1
    configure_docker_proxy

    prompt_domain
    resolve_ssl_mode
    verify_domain_dns || exit 1
    setup_firewall

    setup_ssl_certificate || exit 1
    setup_cert_renewal_cron

    generate_env
    tune_postgres_env .env
    source .env 2>/dev/null || true

    pull_and_start
    wait_for_health
    print_credentials
}

main "$@"
