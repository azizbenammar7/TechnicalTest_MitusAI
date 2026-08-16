#!/bin/sh
set -eu

api_base="${FOOTBALLAI_FRONTEND_API_BASE:-}"
if [ -n "$api_base" ] \
  && ! printf '%s\n' "$api_base" | grep -Eq '^http://(localhost|127[.]0[.]0[.]1):8000$'; then
  echo "FOOTBALLAI_FRONTEND_API_BASE must be blank (same-origin) or a local API URL on port 8000." >&2
  exit 2
fi

printf 'window.__FOOTBALLAI_CONFIG__ = { apiBase: "%s" };\n' "$api_base" \
  > /tmp/footballai-config.js
