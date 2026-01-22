FROM node:20-alpine AS builder

ARG EXT_KEY=""
ENV EXT_KEY=$EXT_KEY
ENV VITE_EXT_ENABLED=${EXT_KEY:+true}

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .
RUN npm run build

FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
