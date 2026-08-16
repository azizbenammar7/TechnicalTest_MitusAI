FROM node:24.13.0-alpine3.23 AS build

WORKDIR /build
COPY v2/dashboard/package.json v2/dashboard/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY v2/dashboard/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29.5-alpine3.23 AS runtime

COPY --chown=nginx:nginx docker/frontend.nginx.conf /etc/nginx/conf.d/default.conf
COPY --chown=nginx:nginx docker/40-footballai-runtime-config.sh /docker-entrypoint.d/40-footballai-runtime-config.sh
COPY --chown=nginx:nginx --from=build /build/dist /usr/share/nginx/html

ENV FOOTBALLAI_FRONTEND_API_BASE=""
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1

USER nginx
