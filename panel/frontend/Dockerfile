# syntax=docker/dockerfile:1.4
FROM node:20-alpine AS builder

ARG EXT_KEY=""
ARG CACHE_BUST=""
ARG NPM_REGISTRY="https://registry.npmmirror.com"
ARG NPM_TIMEOUT=120000
ENV EXT_KEY=$EXT_KEY
ENV VITE_EXT_ENABLED=${EXT_KEY:+true}

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

# Copy source and build
# CACHE_BUST forces rebuild when EXT_KEY changes (even if code is same)
COPY . .
RUN echo "Cache bust: ${CACHE_BUST}" && npm run build

# Production stage - minimal nginx image
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
