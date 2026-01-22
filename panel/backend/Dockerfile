FROM python:3.11-slim

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

# Install system dependencies
# - bash: for running shell scripts
# - curl, git, rsync: for updates
# - openssl: for reading certificate info
# Note: docker CLI is mounted from host via docker-compose
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    rsync \
    openssl \
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

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt || \
    (echo "First pip attempt failed, retrying with Tsinghua mirror..." && \
     pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn -r requirements.txt) || \
    (echo "Second pip attempt failed, retrying with Aliyun mirror..." && \
     pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com -r requirements.txt)

COPY backend/app/ ./app/
COPY backend/ext/ ./ext/
COPY VERSION ./VERSION

RUN mkdir -p /app/data /app/ext/logs

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
