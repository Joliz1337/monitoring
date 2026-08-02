#!/bin/bash
# Generate self-signed SSL certificate for monitoring agent

set -e

SSL_DIR="./nginx/ssl"
DOMAIN="${DOMAIN:-monitoring.local}"
DAYS=365

mkdir -p "$SSL_DIR"

# Check if certificate already exists
if [ -f "$SSL_DIR/cert.pem" ] && [ -f "$SSL_DIR/key.pem" ]; then
    echo "SSL certificate already exists in $SSL_DIR"
    echo "To regenerate, delete existing files first:"
    echo "  rm $SSL_DIR/cert.pem $SSL_DIR/key.pem"
    exit 0
fi

echo "Generating self-signed SSL certificate for $DOMAIN..."

# Generate private key and certificate
openssl req -x509 -nodes -days $DAYS -newkey rsa:2048 \
    -keyout "$SSL_DIR/key.pem" \
    -out "$SSL_DIR/cert.pem" \
    -subj "/C=US/ST=State/L=City/O=Monitoring/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN,DNS:localhost,IP:127.0.0.1"

# Set permissions
chmod 600 "$SSL_DIR/key.pem"
chmod 644 "$SSL_DIR/cert.pem"

echo "SSL certificate generated successfully!"
echo "  Certificate: $SSL_DIR/cert.pem"
echo "  Private key: $SSL_DIR/key.pem"
echo ""
echo "Note: This is a self-signed certificate."
echo "For production, use Let's Encrypt or a proper CA."
