FROM python:3.12-slim

WORKDIR /app

# Use faster mirror for apt packages
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc-dev \
    procps \
    certbot \
    openssl \
    curl \
    iptables \
    git \
    rsync \
    docker.io \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Install Docker Compose V2 plugin
# Try multiple methods: GitHub -> ghfast.top mirror -> with SSL skip (for proxied networks)
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
    && COMPOSE_BIN="/usr/local/lib/docker/cli-plugins/docker-compose" \
    && (echo "Trying GitHub (direct)..." \
        && curl -fsSL --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "$COMPOSE_URL" \
        && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version) \
    || (echo "Trying ghfast.top mirror..." \
        && curl -fsSL --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "https://ghfast.top/$COMPOSE_URL" \
        && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version) \
    || (echo "Trying with SSL skip (proxied network)..." \
        && curl -fsSLk --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "https://ghfast.top/$COMPOSE_URL" \
        && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version) \
    || (echo "Trying GitHub with SSL skip..." \
        && curl -fsSLk --connect-timeout 15 --max-time 180 -o "$COMPOSE_BIN" "$COMPOSE_URL" \
        && chmod +x "$COMPOSE_BIN" && "$COMPOSE_BIN" version) \
    && test -x "$COMPOSE_BIN"

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Copy VERSION file
COPY VERSION ./VERSION

# Create non-root user (but we'll run as root for system access)
# Note: Running as root is required for reading /proc, /sys and managing HAProxy

EXPOSE 7500

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7500"]
