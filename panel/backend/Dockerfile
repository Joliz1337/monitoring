# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ARG CACHE_BUST=""

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash curl git rsync openssl unzip iperf3 proxychains4 gnupg apt-transport-https

# Install Ookla Speedtest CLI
RUN curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash && \
    apt-get install -y --no-install-recommends speedtest || \
    ( ARCH=$(dpkg --print-architecture) && \
      curl -fsSL "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-$([ "$ARCH" = "arm64" ] && echo "aarch64" || echo "x86_64").tgz" \
        -o /tmp/speedtest.tgz && \
      tar xzf /tmp/speedtest.tgz -C /usr/local/bin speedtest && \
      chmod +x /usr/local/bin/speedtest && \
      rm -f /tmp/speedtest.tgz )

# Install xray-core for Xray monitor health checks
ARG XRAY_VERSION=26.2.6
RUN ARCH=$(dpkg --print-architecture) && \
    case "$ARCH" in \
      amd64) XRAY_ARCH="64" ;; \
      arm64) XRAY_ARCH="arm64-v8a" ;; \
      *) XRAY_ARCH="64" ;; \
    esac && \
    curl -fsSL "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-${XRAY_ARCH}.zip" \
      -o /tmp/xray.zip && \
    unzip /tmp/xray.zip xray -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/xray && \
    rm /tmp/xray.zip

# Copy requirements and install Python dependencies with cache
COPY backend/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy application code
# CACHE_BUST forces rebuild when .env changes
COPY backend/app/ ./app/
COPY backend/ext/ ./ext/
COPY VERSION ./VERSION
RUN echo "Build: ${CACHE_BUST:-none}" > /dev/null

RUN mkdir -p /app/data /app/ext/logs

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", \
     "--timeout-keep-alive", "75", \
     "--limit-concurrency", "512", \
     "--backlog", "2048", \
     "--no-server-header"]
