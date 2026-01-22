# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

# Build arguments for configuring mirrors and timeouts
ARG APT_MIRROR="mirror.yandex.ru"
ARG PIP_INDEX_URL="https://pypi.org/simple"
ARG PIP_TIMEOUT=120
ARG APT_TIMEOUT=120
ARG CACHE_BUST=""

# Use faster mirror for apt packages and configure timeouts (single layer)
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    echo 'Acquire::http::Timeout "'${APT_TIMEOUT}'";' > /etc/apt/apt.conf.d/99timeout && \
    echo 'Acquire::https::Timeout "'${APT_TIMEOUT}'";' >> /etc/apt/apt.conf.d/99timeout && \
    echo 'Acquire::Retries "3";' >> /etc/apt/apt.conf.d/99timeout

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    gcc libc-dev procps certbot openssl curl iptables git rsync docker.io dos2unix haproxy

# Configure pip for faster downloads
RUN mkdir -p /root/.config/pip && \
    printf '[global]\ntimeout = %s\nretries = 3\nindex-url = %s\nextra-index-url = https://pypi.org/simple https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple/\ntrusted-host = pypi.org pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com\n' \
    "${PIP_TIMEOUT}" "${PIP_INDEX_URL}" > /root/.config/pip/pip.conf

# Copy requirements and install Python dependencies with cache
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt || \
    (pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt) || \
    (pip install -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com -r requirements.txt)

# Copy application code
# CACHE_BUST forces rebuild when .env changes
COPY app/ ./app/
COPY VERSION ./VERSION
RUN echo "Build: ${CACHE_BUST:-none}" > /dev/null

EXPOSE 7500

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7500"]
