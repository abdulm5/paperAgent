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

echo "Starting PagerAgent, checkout-api, the alert evaluator, and dashboard..."
docker compose up --detach --build backend checkout-api alert-evaluator frontend
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:8100/health" "Checkout API"
wait_for_url "http://localhost:5173" "PagerAgent dashboard"

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
    incident_id="$(printf '%s' "$incidents" | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])')"
    break
  fi
  sleep 0.5
done

if [[ -z "${incident_id:-}" ]]; then
  echo "No incident arrived before the timeout." >&2
  exit 1
fi

echo "Incident $incident_id created. Waiting for its evidence investigation..."
for _ in {1..30}; do
  investigation="$(
    curl --silent --fail \
      "http://localhost:8000/api/v1/incidents/$incident_id/investigations/latest" \
      2>/dev/null || true
  )"
  if [[ -n "$investigation" ]]; then
    investigation_status="$(
      printf '%s' "$investigation" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
    )"
    if [[ "$investigation_status" == "completed" ]]; then
      printf '%s' "$investigation" | python3 -c '
import json
import sys

result = json.load(sys.stdin)
cluster = result["error_clusters"][0]
commit = result["commit_candidates"][0]
runbook = result["runbook_matches"][0]
print("\nPagerAgent investigation complete")
print("  Cluster: {} ({} failures)".format(cluster["error_type"], cluster["failure_count"]))
print("  Suspect: #{} {} (score {:.4f})".format(commit["rank"], commit["commit_sha"], commit["total_score"]))
print("  Runbook: #{} {} (score {:.4f})".format(runbook["rank"], runbook["runbook_id"], runbook["total_score"]))
print("  Evidence: {} immutable artifacts".format(len(result["evidence"])))
'
      echo "Open http://localhost:5173 to inspect citations and advance the response."
      exit 0
    fi
    if [[ "$investigation_status" == "failed" ]]; then
      printf '%s' "$investigation" | python3 -m json.tool >&2
      echo "PagerAgent investigation failed." >&2
      exit 1
    fi
  fi
  sleep 0.5
done

echo "The incident arrived, but its investigation did not finish before the timeout." >&2
exit 1
