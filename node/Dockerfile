# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

ARG CACHE_BUST=""

# Install system dependencies (minimal set for API container)
# - procps: ps command for process listing
# - certbot: SSL certificate generation
# - openssl: SSL utilities
# - curl: health checks
# - iptables: firewall management via API
# - git, rsync: update scripts
# - iperf3: speed test
# psutil и cryptography ставятся из готовых manylinux-wheel, поэтому компилятор не
# нужен — gcc/libc-dev намеренно отсутствуют. На Debian 13 они ещё и ломали сборку:
# apt тянул libc6-dev, жёстко привязанный к более старому libc6, чем в базовом
# образе (point-release skew) — не добавлять их обратно без реальной необходимости.
# Note: haproxy runs on HOST as systemd service, not in container
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    procps certbot openssl curl iptables git rsync iperf3

# Install Docker CLI for accessing remnanode container logs (Xray stats collection)
# Архив сверяется с суммой, записанной здесь: подменённый по дороге бинарник иначе
# попал бы в привилегированный контейнер. Суммы есть только для архитектур ниже —
# на другой сборка падает, а не ставит чужой бинарник. При смене версии суммы
# пересчитать: sha256sum docker-<версия>.tgz
RUN docker_version=26.1.4 && \
    case "$(dpkg --print-architecture)" in \
        amd64) docker_arch=x86_64;  docker_sha256=a9cede81aa3337f310132c2c920dba2edc8d29b7d97065b63ba41cf47ae1ca4f ;; \
        arm64) docker_arch=aarch64; docker_sha256=6f1a5fb161aef875d305ee4f79e65492b3c13e90dbe0a339df2ad6515e4f6849 ;; \
        *) echo "Docker CLI ${docker_version}: no pinned checksum for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${docker_version}.tgz" -o /tmp/docker.tgz && \
    echo "${docker_sha256}  /tmp/docker.tgz" | sha256sum -c - && \
    tar xzf /tmp/docker.tgz -C /tmp docker/docker && \
    mv /tmp/docker/docker /usr/local/bin/ && \
    rm -rf /tmp/docker /tmp/docker.tgz

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

# Агента запускает docker-compose.yml — на локальном сокете (command сервиса api).
# CMD нужен старым compose-файлам без command: панель доставляет новый образ и на
# ноду, чей код на диске не обновился, а там healthcheck и шаблон nginx ждут
# агента на 127.0.0.1:7500. Только петля: network_mode: host, и на 0.0.0.0 API без
# собственной аутентификации торчал бы наружу в обход mTLS-nginx
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "7500"]
