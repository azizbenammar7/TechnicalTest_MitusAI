#!/usr/bin/env bash

# Read-only staging acceptance smoke. The public application FQDN is an ingress
# property; a revision FQDN is not interchangeable with the stable app FQDN.
set -euo pipefail

: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP must be set}"

FRONTEND_APP="${FRONTEND_APP:-ca-footballai-stg-frontend}"
ATTEMPTS="${SMOKE_ATTEMPTS:-12}"
DELAY_SECONDS="${SMOKE_DELAY_SECONDS:-5}"
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

FRONTEND_FQDN=$(az containerapp show \
  --resource-group "$AZURE_RESOURCE_GROUP" \
  --name "$FRONTEND_APP" \
  --query "properties.configuration.ingress.fqdn" \
  --output tsv)

test -n "$FRONTEND_FQDN" || {
  echo "::error::Azure returned an empty stable ingress FQDN for $FRONTEND_APP"
  exit 1
}

echo "Stable frontend FQDN: $FRONTEND_FQDN"

probe_http_200() {
  local path="$1"
  local label="$2"
  local attempt
  local http_code

  for attempt in $(seq 1 "$ATTEMPTS"); do
    http_code=$(curl --silent --show-error \
      --connect-timeout 5 \
      --max-time 20 \
      --output "$BODY_FILE" \
      --write-out '%{http_code}' \
      "https://${FRONTEND_FQDN}${path}" || true)

    if [[ "$http_code" == "200" ]]; then
      echo "$label: HTTP 200"
      cat "$BODY_FILE"
      echo
      return 0
    fi

    echo "::warning::$label attempt $attempt/$ATTEMPTS returned HTTP $http_code"
    if [[ "$attempt" -lt "$ATTEMPTS" ]]; then
      sleep "$DELAY_SECONDS"
    fi
  done

  echo "::error::$label did not return HTTP 200 after $ATTEMPTS attempts"
  return 1
}

probe_api_readiness() {
  local attempt
  local http_code

  for attempt in $(seq 1 "$ATTEMPTS"); do
    http_code=$(curl --silent --show-error \
      --connect-timeout 5 \
      --max-time 20 \
      --output "$BODY_FILE" \
      --write-out '%{http_code}' \
      "https://${FRONTEND_FQDN}/api/ready" || true)

    if [[ "$http_code" == "200" ]] && python3 - "$BODY_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as response_file:
    response = json.load(response_file)

required_checks = ("database", "object_storage", "queue")
checks = response.get("checks", {})
healthy = response.get("status") == "ready" and all(
    checks.get(name) == "ready" for name in required_checks
)
if not healthy:
    print(f"readiness not yet healthy: {response}")
    raise SystemExit(1)
PY
    then
      echo "API /api/ready: HTTP 200 and required cloud dependencies ready"
      cat "$BODY_FILE"
      echo
      return 0
    fi

    echo "::warning::API /api/ready attempt $attempt/$ATTEMPTS was not ready (HTTP $http_code)"
    if [[ "$attempt" -lt "$ATTEMPTS" ]]; then
      sleep "$DELAY_SECONDS"
    fi
  done

  echo "::error::API /api/ready was not healthy after $ATTEMPTS attempts"
  return 1
}

probe_http_200 "/healthz" "Frontend /healthz"
probe_http_200 "/api/health" "API /api/health via frontend proxy"
probe_api_readiness
