FROM python:3.11-slim

WORKDIR /app

# Use faster mirror for apt packages
RUN sed -i 's|deb.debian.org|mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Install system dependencies
# - bash: for running shell scripts
# - curl, git, rsync: for updates
# - openssl: for reading certificate info
# Note: docker CLI is mounted from host via docker-compose
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    rsync \
    openssl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app/ ./app/
COPY VERSION ./VERSION

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
