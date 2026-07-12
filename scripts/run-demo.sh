#!/usr/bin/env bash

set -euo pipefail

AUTO_APPROVE=false
if [[ "${1:-}" == "--approve" ]]; then
  AUTO_APPROVE=true
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--approve]" >&2
  exit 2
fi

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
      investigation_complete=true
      break
    fi
    if [[ "$investigation_status" == "failed" ]]; then
      printf '%s' "$investigation" | python3 -m json.tool >&2
      echo "PagerAgent investigation failed." >&2
      exit 1
    fi
  fi
  sleep 0.5
done

if [[ "${investigation_complete:-false}" != "true" ]]; then
  echo "The incident arrived, but its investigation did not finish before the timeout." >&2
  exit 1
fi

echo "Waiting for the grounded decision packet..."
for _ in {1..30}; do
  proposal="$(
    curl --silent --fail \
      "http://localhost:8000/api/v1/incidents/$incident_id/proposals/latest" \
      2>/dev/null || true
  )"
  if [[ -n "$proposal" ]]; then
    proposal_status="$(
      printf '%s' "$proposal" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
    )"
    if [[ "$proposal_status" == "pending_approval" ]]; then
      break
    fi
  fi
  sleep 0.5
done

if [[ "${proposal_status:-}" != "pending_approval" ]]; then
  echo "The investigation completed, but no approval packet arrived." >&2
  exit 1
fi

printf '%s' "$proposal" | python3 -c '
import json
import sys

result = json.load(sys.stdin)
action = result["action"]
print("\nGrounded copilot brief ready")
print("  Synthesizer: {} ({})".format(result["synthesizer_version"], result["model_name"]))
print("  Confidence: {:.1%}".format(result["confidence"]))
print("  Action: {} {} -> {}".format(action["action_type"], action["target_service"], action["target_release"]))
print("  Claims: {} with evidence citations".format(len(result["claims"])))
'

if [[ "$AUTO_APPROVE" != "true" ]]; then
  echo "Human approval is still required. Open http://localhost:5173, begin the"
  echo "investigation, review the citations, and approve or reject the rollback."
  echo "Run with --approve to exercise the explicit CLI approval path."
  exit 0
fi

incident="$(curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id")"
read -r incident_status incident_version < <(
  printf '%s' "$incident" \
    | python3 -c 'import json, sys; value=json.load(sys.stdin); print(value["status"], value["version"])'
)
if [[ "$incident_status" == "detected" ]]; then
  transition_body="$(python3 -c '
import json
import sys
print(json.dumps({
    "to_status": "investigating",
    "actor": "demo-cli-operator",
    "note": "Reviewed the ranked evidence and grounded decision packet.",
    "expected_version": int(sys.argv[1]),
}))
' "$incident_version")"
  curl --fail --silent --request POST \
    --header "Content-Type: application/json" \
    --data "$transition_body" \
    "http://localhost:8000/api/v1/incidents/$incident_id/transitions" >/dev/null
fi

proposal_id="$(
  printf '%s' "$proposal" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])'
)"
decision_body="$(python3 -c '
import json
print(json.dumps({
    "decision": "approve",
    "actor": "demo-cli-operator",
    "note": "Explicitly approved rollback after reviewing cited evidence.",
}))
')"
result="$(curl --fail --silent --request POST \
  --header "Content-Type: application/json" \
  --data "$decision_body" \
  "http://localhost:8000/api/v1/proposals/$proposal_id/decisions")"

printf '%s' "$result" | python3 -c '
import json
import sys

proposal = json.load(sys.stdin)
execution = proposal["execution"]
response = execution["response_payload"]
print("\nApproved mitigation complete")
print("  Proposal: {}".format(proposal["status"]))
print("  Recovery verified: {}".format(execution["recovery_verified"]))
print("  Canaries: {}".format(response["canary_request_count"]))
print("  Recovery failures: {}".format(response["recovery_failure_count"]))
'

incident="$(curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id")"
read -r incident_status incident_version < <(
  printf '%s' "$incident" \
    | python3 -c 'import json, sys; value=json.load(sys.stdin); print(value["status"], value["version"])'
)
if [[ "$incident_status" != "mitigated" ]]; then
  echo "Recovery passed, but the incident is not in the mitigated state." >&2
  exit 1
fi

resolution_body="$(python3 -c '
import json
import sys
print(json.dumps({
    "to_status": "resolved",
    "actor": "demo-cli-operator",
    "note": "Recovery remained healthy; closing the incident for team review.",
    "expected_version": int(sys.argv[1]),
}))
' "$incident_version")"
curl --fail --silent --request POST \
  --header "Content-Type: application/json" \
  --data "$resolution_body" \
  "http://localhost:8000/api/v1/incidents/$incident_id/transitions" >/dev/null

echo "Incident resolved. Waiting for the grounded postmortem draft..."
for _ in {1..30}; do
  postmortem="$(
    curl --silent --fail \
      "http://localhost:8000/api/v1/incidents/$incident_id/postmortem" \
      2>/dev/null || true
  )"
  if [[ -n "$postmortem" ]]; then
    postmortem_status="$(
      printf '%s' "$postmortem" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
    )"
    if [[ "$postmortem_status" == "draft" ]]; then
      break
    fi
  fi
  sleep 0.5
done

if [[ "${postmortem_status:-}" != "draft" ]]; then
  echo "The incident resolved, but its postmortem was not generated before the timeout." >&2
  exit 1
fi

read -r postmortem_id postmortem_version < <(
  printf '%s' "$postmortem" \
    | python3 -c 'import json, sys; value=json.load(sys.stdin); print(value["id"], value["version"])'
)
printf '%s' "$postmortem" | python3 -c '
import json
import sys

report = json.load(sys.stdin)
content = report["content"]
print("\nGrounded postmortem draft ready")
print("  Generator: {} ({})".format(report["generator_version"], report["model_name"]))
print("  Version: v{} ({})".format(report["version"], report["status"]))
print("  Timeline: {} exact incident events".format(len(content["timeline"])))
print("  Prevention: {} assigned actions".format(len(content["prevention_items"])))
print("  Revisions: {} immutable snapshots".format(len(report["revisions"])))
'

export_path="${TMPDIR:-/tmp}/pageragent-$incident_id.md"
curl --fail --silent \
  "http://localhost:8000/api/v1/postmortems/$postmortem_id/export" \
  --output "$export_path"
echo "Markdown export verified at $export_path"
echo "Open http://localhost:5173 to edit, review, and finalize the case file."
