FROM node:20-alpine AS builder

ARG EXT_KEY=""
ARG NPM_REGISTRY="https://registry.npmmirror.com"
ARG NPM_TIMEOUT=120000
ENV EXT_KEY=$EXT_KEY
ENV VITE_EXT_ENABLED=${EXT_KEY:+true}

WORKDIR /app

# Configure npm for faster downloads with mirrors and timeouts
RUN npm config set registry ${NPM_REGISTRY} && \
    npm config set fetch-timeout ${NPM_TIMEOUT} && \
    npm config set fetch-retries 3 && \
    npm config set fetch-retry-mintimeout 10000 && \
    npm config set fetch-retry-maxtimeout 60000

COPY package.json ./

# Install with fallback to official registry
RUN npm install --prefer-offline --no-audit || \
    (echo "First npm attempt failed, retrying with npmmirror..." && \
     npm config set registry https://registry.npmmirror.com && \
     npm install --prefer-offline --no-audit) || \
    (echo "Second npm attempt failed, retrying with official registry..." && \
     npm config set registry https://registry.npmjs.org && \
     npm install --prefer-offline --no-audit)

# Copy source and build
COPY . .

# Debug: check if prebuild.js has navItem fix
RUN echo "=== prebuild.js navItem check ===" && grep -n "navItem" scripts/prebuild.js || echo "navItem NOT FOUND in prebuild.js"

# Debug: show _extStore.ts BEFORE prebuild
RUN echo "=== _extStore.ts BEFORE prebuild ===" && cat src/stores/_extStore.ts

# Run prebuild
RUN npm run prebuild

# Debug: show _extStore.ts AFTER prebuild
RUN echo "=== _extStore.ts AFTER prebuild ===" && cat src/stores/_extStore.ts

# Build TypeScript and Vite
RUN npx tsc && npx vite build

FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
