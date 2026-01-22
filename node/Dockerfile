# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

ARG CACHE_BUST=""

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc libc-dev procps certbot openssl curl iptables git rsync docker.io dos2unix haproxy

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
