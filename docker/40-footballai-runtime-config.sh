#!/bin/sh
set -eu

api_base="${FOOTBALLAI_FRONTEND_API_BASE:-}"
api_upstream="${FOOTBALLAI_API_UPSTREAM:-http://api:8000}"
upload_mode="${FOOTBALLAI_FRONTEND_UPLOAD_MODE:-multipart}"
blob_connect_src="${FOOTBALLAI_BLOB_CONNECT_SRC:-}"
environment="${FOOTBALLAI_ENVIRONMENT:-local}"
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

if ! printf '%s\n' "$upload_mode" | grep -Eq '^(multipart|direct)$'; then
  echo "FOOTBALLAI_FRONTEND_UPLOAD_MODE must be multipart or direct." >&2
  exit 2
fi

if [ -n "$blob_connect_src" ] \
  && ! printf '%s\n' "$blob_connect_src" | grep -Eq '^https://[a-z0-9]+[.]blob[.]core[.]windows[.]net$'; then
  echo "FOOTBALLAI_BLOB_CONNECT_SRC must be blank or an Azure Blob HTTPS account origin." >&2
  exit 2
fi

if [ "$upload_mode" = "direct" ] && [ -z "$blob_connect_src" ]; then
  echo "Direct upload requires FOOTBALLAI_BLOB_CONNECT_SRC." >&2
  exit 2
fi

if ! printf '%s\n' "$environment" | grep -Eq '^(local|staging|production|test)$'; then
  echo "FOOTBALLAI_ENVIRONMENT must be local, staging, production, or test." >&2
  exit 2
fi

printf 'window.__FOOTBALLAI_CONFIG__ = { apiBase: "%s", uploadMode: "%s" };\n' "$api_base" "$upload_mode" \
  > /tmp/footballai-config.js

sed -e "s|__FOOTBALLAI_API_UPSTREAM__|$api_upstream|g" \
  -e "s|__FOOTBALLAI_BLOB_CONNECT_SRC__|$blob_connect_src|g" \
  -e "s|__FOOTBALLAI_ENVIRONMENT__|$environment|g" \
  /etc/nginx/footballai-nginx.conf.template \
  > /tmp/footballai-nginx.conf
