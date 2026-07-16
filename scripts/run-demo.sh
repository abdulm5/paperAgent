#!/usr/bin/env bash

set -euo pipefail

AUTO_APPROVE=false
SCENARIO="checkout-validation-bug"
WORKFLOW_WAIT_ATTEMPTS="${PAGERAGENT_DEMO_WORKFLOW_WAIT_ATTEMPTS:-240}"
RUNTIME_SERVICES_QUIESCED=false
WORKFLOW_STREAM_NAME=""
WORKFLOW_DEAD_LETTER_STREAM=""
AUTH_TOKEN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --approve)
      AUTO_APPROVE=true
      shift
      ;;
    --scenario)
      SCENARIO="${2:-}"
      shift 2
      ;;
    *)
      echo "Usage: $0 [--approve] [--scenario SCENARIO_ID]" >&2
      exit 2
      ;;
  esac
done

if ! [[ "$WORKFLOW_WAIT_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  echo "PAGERAGENT_DEMO_WORKFLOW_WAIT_ATTEMPTS must be a positive integer." >&2
  exit 2
fi

case "$SCENARIO" in
  checkout-validation-bug|payment-provider-timeout|checkout-feature-flag-regression) ;;
  *)
    echo "Unknown scenario: $SCENARIO" >&2
    exit 2
    ;;
esac

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

wait_for_redis() {
  for _ in {1..30}; do
    if [[ "$(docker compose exec -T redis redis-cli --raw PING 2>/dev/null || true)" == "PONG" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Redis did not become ready." >&2
  return 1
}

authenticate_demo() {
  local response
  response="$(
    curl --fail --silent --request POST \
      --header "Content-Type: application/json" \
      --data '{"persona":"admin","organization_slug":"pageragent-labs"}' \
      http://localhost:8000/api/v1/auth/dev/session
  )"
  AUTH_TOKEN="$(
    printf '%s' "$response" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
  )"
  if [[ -z "$AUTH_TOKEN" ]]; then
    echo "PagerAgent did not issue the local demo session." >&2
    return 1
  fi
}

api_curl() {
  curl --header "Authorization: Bearer $AUTH_TOKEN" "$@"
}

restore_runtime() {
  if [[ "$RUNTIME_SERVICES_QUIESCED" != "true" ]]; then
    return
  fi
  docker compose up --detach \
    alert-evaluator outbox-relay workflow-worker >/dev/null 2>&1 || true
  RUNTIME_SERVICES_QUIESCED=false
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  restore_runtime
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

echo "Quiescing any workflow runtime left by an earlier demo..."
docker compose stop \
  alert-evaluator outbox-relay workflow-worker >/dev/null 2>&1 || true
RUNTIME_SERVICES_QUIESCED=true

echo "Starting PagerAgent core services, simulator, Redis, and dashboard..."
docker compose up --detach --build backend checkout-api redis frontend
docker compose build alert-evaluator outbox-relay workflow-worker
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:8100/health" "Checkout API"
wait_for_url "http://localhost:5173" "PagerAgent dashboard"
wait_for_redis
authenticate_demo

stream_names="$(
  docker compose exec -T backend python -c '
from app.core.config import settings

print(f"{settings.workflow_stream_name}|{settings.workflow_dead_letter_stream}")
'
)"
IFS='|' read -r WORKFLOW_STREAM_NAME WORKFLOW_DEAD_LETTER_STREAM <<<"$stream_names"
if [[ -z "$WORKFLOW_STREAM_NAME" || -z "$WORKFLOW_DEAD_LETTER_STREAM" ]]; then
  echo "Could not resolve workflow stream names from the runtime configuration." >&2
  exit 1
fi

echo "Resetting previous demo state..."
api_curl --fail --silent --request DELETE http://localhost:8000/api/v1/dev/incidents >/dev/null
curl --fail --silent --request POST http://localhost:8100/admin/reset >/dev/null
docker compose exec -T redis redis-cli DEL \
  "$WORKFLOW_STREAM_NAME" "$WORKFLOW_DEAD_LETTER_STREAM" >/dev/null

echo "Starting a clean alert evaluator, outbox relay, and workflow worker..."
docker compose up --detach alert-evaluator outbox-relay workflow-worker
RUNTIME_SERVICES_QUIESCED=false

echo "Sending 20 healthy checkout requests..."
docker compose --profile tools run --rm traffic-generator \
  --requests 20 --delay 0 --run-id healthy

echo "Activating versioned scenario $SCENARIO..."
curl --fail --silent --request POST \
  "http://localhost:8100/admin/scenarios/$SCENARIO/activate" | python3 -m json.tool

echo "Sending 40 requests through the active failure mode..."
docker compose --profile tools run --rm traffic-generator \
  --requests 40 --delay 0 --run-id outage

echo "Waiting for the 5% error-rate alert..."
for _ in {1..20}; do
  incidents="$(api_curl --fail --silent http://localhost:8000/api/v1/incidents)"
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
for ((attempt = 1; attempt <= WORKFLOW_WAIT_ATTEMPTS; attempt += 1)); do
  investigation="$(
    api_curl --silent --fail \
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
cause = result["cause_candidates"][0]
commit = result["commit_candidates"][0]
runbook = result["runbook_matches"][0]
metric_snapshot = next(
    (item for item in result["evidence"] if item["kind"] == "prometheus_metric_snapshot"),
    None,
)
print("\nPagerAgent investigation complete")
print("  Cluster: {} ({} failures)".format(cluster["error_type"], cluster["failure_count"]))
print("  Cause: #{} {} / {} (score {:.4f})".format(cause["rank"], cause["kind"], cause["reference"], cause["score"]))
print("  Deploy candidate: #{} {} (score {:.4f})".format(commit["rank"], commit["commit_sha"], commit["total_score"]))
print("  Runbook: #{} {} (score {:.4f})".format(runbook["rank"], runbook["runbook_id"], runbook["total_score"]))
print("  Evidence: {} immutable artifacts".format(len(result["evidence"])))
if metric_snapshot:
    payload = metric_snapshot["payload"]
    print(
        "  Prometheus: {} samples across {} series ({})".format(
            payload["sample_count"], payload["series_count"], payload["query_id"]
        )
    )
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
for ((attempt = 1; attempt <= WORKFLOW_WAIT_ATTEMPTS; attempt += 1)); do
  proposal="$(
    api_curl --silent --fail \
      "http://localhost:8000/api/v1/incidents/$incident_id/proposals/latest" \
      2>/dev/null || true
  )"
  if [[ -n "$proposal" ]]; then
    proposal_status="$(
      printf '%s' "$proposal" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
    )"
    if [[ "$proposal_status" == "pending_approval" || "$proposal_status" == "advisory" ]]; then
      break
    fi
  fi
  sleep 0.5
done

if [[ "${proposal_status:-}" != "pending_approval" && "${proposal_status:-}" != "advisory" ]]; then
  echo "The investigation completed, but no decision packet arrived." >&2
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
target = action.get("target_release") or action.get("feature_flag") or "owning-service escalation"
print("  Action: {} {} -> {}".format(action["action_type"], action["target_service"], target))
print("  Automation allowed: {}".format(action["automation_allowed"]))
print("  Claims: {} with evidence citations".format(len(result["claims"])))
'

if [[ "$proposal_status" == "advisory" ]]; then
  echo "PagerAgent stopped at the write boundary and produced an advisory-only escalation."
  echo "Open http://localhost:5173 to inspect why the nearby deploy was rejected as causal."
  exit 0
fi

if [[ "$AUTO_APPROVE" != "true" ]]; then
  echo "Human approval is still required. Open http://localhost:5173, begin the"
  echo "investigation, review the citations, and approve or reject the typed action."
  echo "Run with --approve to exercise the explicit CLI approval path."
  exit 0
fi

incident="$(api_curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id")"
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
    "note": "Reviewed the ranked evidence and grounded decision packet.",
    "expected_version": int(sys.argv[1]),
}))
' "$incident_version")"
  api_curl --fail --silent --request POST \
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
    "note": "Explicitly approved the typed action after reviewing cited evidence.",
}))
')"
approval="$(api_curl --fail --silent --request POST \
  --header "Content-Type: application/json" \
  --data "$decision_body" \
  "http://localhost:8000/api/v1/proposals/$proposal_id/decisions")"

approval_status="$(
  printf '%s' "$approval" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
)"
if [[ "$approval_status" != "approved" && "$approval_status" != "verification_passed" ]]; then
  printf '%s' "$approval" | python3 -m json.tool >&2
  echo "Proposal approval did not enter durable execution." >&2
  exit 1
fi

echo "Approval recorded. Waiting for the durable mitigation worker..."
for ((attempt = 1; attempt <= WORKFLOW_WAIT_ATTEMPTS; attempt += 1)); do
  result="$(
    api_curl --silent --fail \
      "http://localhost:8000/api/v1/incidents/$incident_id/proposals/latest" \
      2>/dev/null || true
  )"
  if [[ -n "$result" ]]; then
    result_status="$(
      printf '%s' "$result" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
    )"
    if [[ "$result_status" == "verification_passed" ]]; then
      break
    fi
    if [[ "$result_status" == "execution_failed" ]]; then
      printf '%s' "$result" | python3 -m json.tool >&2
      echo "Durable mitigation exhausted or failed verification." >&2
      exit 1
    fi
  fi
  sleep 0.5
done

if [[ "${result_status:-}" != "verification_passed" ]]; then
  echo "The mitigation did not finish before the timeout." >&2
  exit 1
fi

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

incident="$(api_curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id")"
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
    "note": "Recovery remained healthy; closing the incident for team review.",
    "expected_version": int(sys.argv[1]),
}))
' "$incident_version")"
api_curl --fail --silent --request POST \
  --header "Content-Type: application/json" \
  --data "$resolution_body" \
  "http://localhost:8000/api/v1/incidents/$incident_id/transitions" >/dev/null

echo "Incident resolved. Waiting for the grounded postmortem draft..."
for ((attempt = 1; attempt <= WORKFLOW_WAIT_ATTEMPTS; attempt += 1)); do
  postmortem="$(
    api_curl --silent --fail \
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
api_curl --fail --silent \
  "http://localhost:8000/api/v1/postmortems/$postmortem_id/export" \
  --output "$export_path"
echo "Markdown export verified at $export_path"
echo "Open http://localhost:5173 to edit, review, and finalize the case file."
