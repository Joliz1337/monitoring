# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

ARG CACHE_BUST=""

# Install system dependencies (minimal set for API container)
# - gcc, libc-dev: compile Python C extensions (psutil)
# - procps: ps command for process listing
# - certbot: SSL certificate generation
# - openssl: SSL utilities
# - curl: health checks
# - iptables: firewall management via API
# - git, rsync: update scripts
# Note: haproxy runs on HOST as systemd service, not in container
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc libc-dev procps certbot openssl curl iptables git rsync

# Install Docker CLI for accessing remnanode container logs (Xray stats collection)
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz | tar xz -C /tmp && \
    mv /tmp/docker/docker /usr/local/bin/ && \
    rm -rf /tmp/docker

# Copy requirements and install Python dependencies with cache
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy application code
# CACHE_BUST forces rebuild when .env changes
COPY app/ ./app/
COPY VERSION ./VERSION
RUN echo "Build: ${CACHE_BUST:-none}" > /dev/null

EXPOSE 7500

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7500"]
