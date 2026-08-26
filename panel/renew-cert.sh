#!/bin/bash
#
# Продление SSL-сертификата панели. Запускается бэкендом из контейнера (кнопка
# «Продлить» в Настройках) или вручную с хоста из /opt/monitoring-panel.
#
# Способ продления берётся из renewal-конфига certbot:
#   standalone     — certbot в контейнере certbot/certbot занимает порт 80,
#                    panel-nginx на это время останавливается
#   dns-cloudflare — certbot с плагином Cloudflare (есть в образе бэкенда, на хост
#                    ставится установщиком); nginx не останавливается, после
#                    продления перечитывает сертификат
#
# Usage: renew-cert.sh [--force]
# Exit: 0 — продлён, 2 — срок ещё не подошёл, 1 — ошибка

# Без set -e: коды возврата certbot разбираются вручную, а падение
# `OUTPUT=$(...)` при set -e оборвало бы скрипт до старта nginx

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_RENEWAL=""

for arg in "$@"; do
    [ "$arg" = "--force" ] && FORCE_RENEWAL="--force-renewal"
done

[ -f "${SCRIPT_DIR}/.env" ] && source "${SCRIPT_DIR}/.env"

if [ -z "$DOMAIN" ]; then
    echo "ERROR: DOMAIN not set in .env"
    exit 1
fi

CERT_PATH="/etc/letsencrypt/live/${DOMAIN}"
RENEWAL_CONF="/etc/letsencrypt/renewal/${DOMAIN}.conf"

if [ ! -f "${CERT_PATH}/fullchain.pem" ]; then
    echo "ERROR: Certificate not found at ${CERT_PATH}"
    exit 1
fi

AUTHENTICATOR=$(sed -n 's/^authenticator *= *//p' "$RENEWAL_CONF" 2>/dev/null | head -1)

echo "Renewing certificate for ${DOMAIN} (${AUTHENTICATOR:-standalone})..."
[ -n "$FORCE_RENEWAL" ] && echo "Force mode: a new certificate is issued regardless of the expiry date"

renew_dns_cloudflare() {
    if ! command -v certbot >/dev/null 2>&1; then
        echo "ERROR: certbot with the Cloudflare plugin is not available here"
        return 1
    fi
    # shellcheck disable=SC2086
    certbot renew --cert-name "$DOMAIN" --non-interactive $FORCE_RENEWAL 2>&1
}

# Стандартный режим — certbot renew; принудительный — certonly --standalone,
# он выпускает новую линию даже при повреждённом renewal-конфиге
renew_standalone() {
    echo "Stopping nginx..."
    docker stop panel-nginx 2>/dev/null || true
    # nginx обязан подняться при любом исходе, иначе панель останется недоступной
    trap 'docker start panel-nginx >/dev/null 2>&1 || true' EXIT
    sleep 2

    local docker_args=(run --rm --name certbot-renew
        -v /etc/letsencrypt:/etc/letsencrypt
        -v /var/lib/letsencrypt:/var/lib/letsencrypt
        -p 80:80 certbot/certbot)

    if [ -n "$FORCE_RENEWAL" ]; then
        docker "${docker_args[@]}" certonly --standalone --non-interactive --agree-tos \
            --register-unsafely-without-email --force-renewal -d "$DOMAIN" 2>&1
    else
        docker "${docker_args[@]}" renew --cert-name "$DOMAIN" --non-interactive 2>&1
    fi
}

if [ "$AUTHENTICATOR" = "dns-cloudflare" ]; then
    CERTBOT_OUTPUT=$(renew_dns_cloudflare)
else
    CERTBOT_OUTPUT=$(renew_standalone)
fi
CERTBOT_EXIT=$?

echo "Certbot output:"
echo "$CERTBOT_OUTPUT"
echo ""

if echo "$CERTBOT_OUTPUT" | grep -qE "(Successfully received|Congratulations|successfully renewed|new certificate)"; then
    echo "Certificate renewed successfully!"
    RESULT=0
elif echo "$CERTBOT_OUTPUT" | grep -qE "(not yet due for renewal|No renewals were attempted)"; then
    echo "Certificate is not due for renewal yet."
    RESULT=2
elif [ $CERTBOT_EXIT -ne 0 ]; then
    echo "Certificate renewal failed"
    RESULT=1
else
    echo "Certificate check completed"
    RESULT=0
fi

if [ "$AUTHENTICATOR" = "dns-cloudflare" ]; then
    [ $RESULT -eq 0 ] && { echo "Reloading nginx..."; docker exec panel-nginx nginx -s reload 2>/dev/null || true; }
else
    echo "Starting nginx..."
    docker start panel-nginx 2>/dev/null || true
fi

exit $RESULT
