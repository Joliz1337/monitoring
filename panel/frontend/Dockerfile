# syntax=docker/dockerfile:1.4
FROM node:20-alpine AS builder

ARG CACHE_BUST=""
ARG NPM_REGISTRY="https://registry.npmmirror.com"
ARG NPM_TIMEOUT=120000

WORKDIR /app

# Configure npm for faster downloads
RUN npm config set registry ${NPM_REGISTRY} && \
    npm config set fetch-timeout ${NPM_TIMEOUT} && \
    npm config set fetch-retries 3 && \
    npm config set fetch-retry-mintimeout 10000 && \
    npm config set fetch-retry-maxtimeout 60000

# Copy package files first for better caching
COPY package.json package-lock.json* ./

# Install dependencies with npm cache mount
RUN --mount=type=cache,target=/root/.npm \
    npm install --prefer-offline --no-audit || \
    (npm config set registry https://registry.npmmirror.com && npm install --prefer-offline --no-audit) || \
    (npm config set registry https://registry.npmjs.org && npm install --prefer-offline --no-audit)

COPY . .
RUN echo "Cache bust: ${CACHE_BUST}" && npm run build

FROM nginx:alpine

RUN apk add --no-cache python3 py3-cryptography

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY ext-dist.enc* /opt/
COPY decrypt_dist.py /opt/decrypt_dist.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
