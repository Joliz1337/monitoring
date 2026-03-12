# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ARG CACHE_BUST=""

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash curl git rsync openssl unzip

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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
