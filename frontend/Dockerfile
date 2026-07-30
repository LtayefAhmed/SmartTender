# syntax=docker/dockerfile:1.7
# =============================================================================
# Frontend image: build the static bundle, serve it with nginx.
#
# nginx also reverse-proxies /api to the backend, so the browser makes
# same-origin requests and there is no CORS to configure. The API base and key
# are injected into index.html at container start (entrypoint), which is what
# lets one built image run against any environment.
# =============================================================================
FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY tsconfig*.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
RUN npm run build

# -----------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

RUN apk add --no-cache gettext   # provides envsubst for runtime config

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.d/40-smarttender-config.sh
RUN chmod +x /docker-entrypoint.d/40-smarttender-config.sh

EXPOSE 80

# 127.0.0.1, not localhost: in the alpine container `localhost` can resolve to
# ::1 (IPv6) first, but nginx `listen 80` binds IPv4 only, so the probe would
# get "connection refused" while the service is perfectly healthy.
HEALTHCHECK --interval=30s --timeout=4s --start-period=8s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz >/dev/null 2>&1 || exit 1
