#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

wait_for_url() {
  local url="$1"
  local label="$2"
  for _ in {1..30}; do
    if curl --fail --silent "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "$label did not become ready." >&2
  return 1
}

echo "Starting PagerAgent, checkout-api, and the alert evaluator..."
docker compose up --detach --build backend checkout-api alert-evaluator
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:8100/health" "Checkout API"

echo "Resetting previous demo state..."
curl --fail --silent --request DELETE http://localhost:8000/api/v1/dev/incidents >/dev/null
curl --fail --silent --request POST http://localhost:8100/admin/reset >/dev/null

echo "Sending 20 healthy checkout requests..."
docker compose --profile tools run --rm traffic-generator \
  --requests 20 --delay 0 --run-id healthy

echo "Deploying faulty-v2 (commit 8fa23c1)..."
curl --fail --silent --request POST \
  http://localhost:8100/admin/releases/faulty-v2/activate | python3 -m json.tool

echo "Sending 40 requests against the faulty release..."
docker compose --profile tools run --rm traffic-generator \
  --requests 40 --delay 0 --run-id outage

echo "Waiting for the 5% error-rate alert..."
for _ in {1..20}; do
  incidents="$(curl --fail --silent http://localhost:8000/api/v1/incidents)"
  if [[ "$incidents" != "[]" ]]; then
    printf '%s' "$incidents" | python3 -m json.tool
    echo "Demo complete: PagerAgent received the incident."
    exit 0
  fi
  sleep 0.5
done

echo "No incident arrived before the timeout." >&2
exit 1
