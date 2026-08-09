# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ARG CACHE_BUST=""

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash curl git rsync openssl haproxy nginx

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

# --forwarded-allow-ips перечисляет доверенных прокси, а не клиентов: nginx панели
# ходит к бэкенду из bridge-сети Docker, поэтому 127.0.0.1 здесь не срабатывал —
# uvicorn не разбирал X-Forwarded-For и все клиенты выглядели одним IP контейнера.
# Порт 8000 наружу не опубликован, снаружи сети Docker сюда не постучаться.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--loop", "uvloop", "--http", "httptools", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10", \
     "--timeout-keep-alive", "75", \
     "--limit-concurrency", "512", \
     "--backlog", "2048", \
     "--no-server-header"]
