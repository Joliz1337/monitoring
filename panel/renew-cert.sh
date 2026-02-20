#!/bin/bash

# SSL Certificate Renewal Script for Monitoring Panel
# This script renews the Let's Encrypt certificate for the panel domain
# Runs certbot via Docker container since we're executing from within a container
#
# Usage: renew-cert.sh [--force]
#   --force: Force renewal even if certificate is not due

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORCE_RENEWAL=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --force)
            FORCE_RENEWAL="--force-renewal"
            echo "Force renewal mode enabled"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# Load domain from .env
if [ -f "${SCRIPT_DIR}/.env" ]; then
    source "${SCRIPT_DIR}/.env"
fi

if [ -z "$DOMAIN" ]; then
    echo "ERROR: DOMAIN not set in .env"
    exit 1
fi

CERT_PATH="/etc/letsencrypt/live/${DOMAIN}"

# Check if certificate exists
if [ ! -f "${CERT_PATH}/fullchain.pem" ]; then
    echo "ERROR: Certificate not found at ${CERT_PATH}"
    exit 1
fi

echo "Renewing certificate for ${DOMAIN}..."
if [ -n "$FORCE_RENEWAL" ]; then
    echo "WARNING: Force renewal will create a new certificate regardless of expiration date"
fi

# Stop nginx to free port 80
echo "Stopping nginx..."
docker stop panel-nginx 2>/dev/null || true
sleep 2

# Run certbot via Docker
# Using certbot/certbot official image
CERTBOT_OUTPUT=""
RESULT=0

if [ -n "$FORCE_RENEWAL" ]; then
    # Force mode: use certonly --standalone like on node (more reliable)
    echo "Running certbot certonly --standalone --force-renewal..."
    CERTBOT_OUTPUT=$(docker run --rm \
        --name certbot-renew \
        -v /etc/letsencrypt:/etc/letsencrypt \
        -v /var/lib/letsencrypt:/var/lib/letsencrypt \
        -p 80:80 \
        certbot/certbot certonly \
            --standalone \
            --non-interactive \
            --agree-tos \
            --register-unsafely-without-email \
            --force-renewal \
            -d "$DOMAIN" 2>&1)
    CERTBOT_EXIT=$?
else
    # Normal mode: use renew (only renews if <30 days left)
    echo "Running certbot renew..."
    CERTBOT_OUTPUT=$(docker run --rm \
        --name certbot-renew \
        -v /etc/letsencrypt:/etc/letsencrypt \
        -v /var/lib/letsencrypt:/var/lib/letsencrypt \
        -p 80:80 \
        certbot/certbot renew --cert-name "$DOMAIN" --non-interactive 2>&1)
    CERTBOT_EXIT=$?
fi

echo "Certbot output:"
echo "$CERTBOT_OUTPUT"

# Check output to determine actual result
if echo "$CERTBOT_OUTPUT" | grep -qE "(Successfully received|Congratulations|successfully renewed|new certificate)"; then
    echo ""
    echo "Certificate renewed successfully!"
    RESULT=0
elif echo "$CERTBOT_OUTPUT" | grep -q "not yet due for renewal"; then
    echo ""
    echo "Certificate is not due for renewal yet."
    RESULT=2  # Special code for "not due"
elif [ $CERTBOT_EXIT -eq 0 ] && echo "$CERTBOT_OUTPUT" | grep -q "No renewals were attempted"; then
    echo ""
    echo "Certificate is not due for renewal yet."
    RESULT=2
elif [ $CERTBOT_EXIT -ne 0 ]; then
    echo ""
    echo "Certificate renewal failed"
    RESULT=1
else
    # Certbot returned 0 but we're not sure what happened
    echo ""
    echo "Certificate check completed"
    RESULT=0
fi

# Start nginx back
echo "Starting nginx..."
docker start panel-nginx 2>/dev/null || true

exit $RESULT
