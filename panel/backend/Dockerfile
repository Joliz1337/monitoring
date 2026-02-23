# syntax=docker/dockerfile:1.4
FROM python:3.11-slim

WORKDIR /app

ARG CACHE_BUST=""

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash curl git rsync openssl

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
