#!/bin/sh
set -eu

api_base="${FOOTBALLAI_FRONTEND_API_BASE:-}"
api_upstream="${FOOTBALLAI_API_UPSTREAM:-http://api:8000}"
if [ -n "$api_base" ] \
  && ! printf '%s\n' "$api_base" | grep -Eq '^http://(localhost|127[.]0[.]0[.]1):8000$'; then
  echo "FOOTBALLAI_FRONTEND_API_BASE must be blank (same-origin) or a local API URL on port 8000." >&2
  exit 2
fi

if ! printf '%s\n' "$api_upstream" \
  | grep -Eq '^http://api:8000$|^https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?$'; then
  echo "FOOTBALLAI_API_UPSTREAM must be http://api:8000 or an HTTPS hostname." >&2
  exit 2
fi

printf 'window.__FOOTBALLAI_CONFIG__ = { apiBase: "%s" };\n' "$api_base" \
  > /tmp/footballai-config.js

sed "s|__FOOTBALLAI_API_UPSTREAM__|$api_upstream|g" \
  /etc/nginx/footballai-nginx.conf.template \
  > /tmp/footballai-nginx.conf
