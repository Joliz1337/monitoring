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

# Install Docker Compose V2 plugin with mirror fallback for Russia
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && COMPOSE_URL="https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
    && MIRRORS="https://ghproxy.com/$COMPOSE_URL https://mirror.ghproxy.com/$COMPOSE_URL https://github.moeyy.xyz/$COMPOSE_URL $COMPOSE_URL" \
    && for mirror in $MIRRORS; do \
         echo "Trying: $(echo $mirror | cut -d'/' -f3)..." \
         && curl -fsSL --connect-timeout 10 --max-time 120 -o /usr/local/lib/docker/cli-plugins/docker-compose "$mirror" \
         && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
         && break || echo "Failed, trying next..."; \
       done \
    && test -x /usr/local/lib/docker/cli-plugins/docker-compose || exit 1

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
