FROM python:3.12-slim

WORKDIR /app

# Build arguments for configuring mirrors and timeouts
ARG APT_MIRROR="mirror.yandex.ru"
ARG PIP_INDEX_URL="https://pypi.org/simple"
ARG PIP_TIMEOUT=120
ARG APT_TIMEOUT=120

# Use faster mirror for apt packages (configurable)
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Configure apt timeouts and retries
RUN echo 'Acquire::http::Timeout "'${APT_TIMEOUT}'";' > /etc/apt/apt.conf.d/99timeout && \
    echo 'Acquire::https::Timeout "'${APT_TIMEOUT}'";' >> /etc/apt/apt.conf.d/99timeout && \
    echo 'Acquire::Retries "3";' >> /etc/apt/apt.conf.d/99timeout

# Install system dependencies with timeout
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
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
    haproxy \
    && rm -rf /var/lib/apt/lists/* && \
    apt-get clean

# Configure pip for faster downloads with mirrors and timeouts
RUN mkdir -p /root/.config/pip && \
    echo "[global]" > /root/.config/pip/pip.conf && \
    echo "timeout = ${PIP_TIMEOUT}" >> /root/.config/pip/pip.conf && \
    echo "retries = 3" >> /root/.config/pip/pip.conf && \
    echo "index-url = ${PIP_INDEX_URL}" >> /root/.config/pip/pip.conf && \
    echo "extra-index-url = https://pypi.org/simple https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple/" >> /root/.config/pip/pip.conf && \
    echo "trusted-host = pypi.org pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com" >> /root/.config/pip/pip.conf

# Copy requirements and install Python dependencies with retries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || \
    (echo "First pip attempt failed, retrying with Tsinghua mirror..." && \
     pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt) || \
    (echo "Second pip attempt failed, retrying with Aliyun mirror..." && \
     pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com -r requirements.txt)

# Copy application code
COPY app/ ./app/

# Copy VERSION file
COPY VERSION ./VERSION

# Create non-root user (but we'll run as root for system access)
# Note: Running as root is required for reading /proc, /sys and managing HAProxy

EXPOSE 7500

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7500"]
