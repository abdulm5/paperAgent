#!/usr/bin/env bash

set -euo pipefail

AUTO_APPROVE=false
BUILD_IMAGES=true
SIMULATE_REDIS_OUTAGE=true
STEP_NUMBER=0
STACK_STARTED=false
REDIS_STOPPED=false
ALERT_EVALUATOR_STOPPED=false
WORKFLOW_SERVICES_STOPPED=false

WORKFLOW_STREAM_NAME=""
WORKFLOW_DEAD_LETTER_STREAM=""
AUTH_TOKEN=""

usage() {
  cat <<'EOF'
Usage: ./scripts/run-durability-demo.sh [options]

Demonstrate that PagerAgent stores workflow intent in PostgreSQL, recovers after
Redis and worker downtime, repairs a lost stream, and treats duplicate delivery
as a no-op.

Options:
  --approve          Continue through human approval and durable mitigation.
  --no-build         Reuse existing Docker images instead of rebuilding them.
  --keep-redis-up    Stop only the relay and worker; leave Redis available.
  -h, --help         Show this help text.

The script resets PagerAgent's local demo records and simulator telemetry. It
never removes containers or Docker volumes. On failure it restores Redis, the
alert evaluator, outbox relay, and workflow worker before exiting.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --approve)
      AUTO_APPROVE=true
      shift
      ;;
    --no-build)
      BUILD_IMAGES=false
      shift
      ;;
    --keep-redis-up)
      SIMULATE_REDIS_OUTAGE=false
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

step() {
  STEP_NUMBER=$((STEP_NUMBER + 1))
  printf '\n[%02d] %s\n' "$STEP_NUMBER" "$1"
}

fail() {
  echo "Durability demo failed: $1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' is unavailable."
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"
  local index
  for ((index = 1; index <= attempts; index += 1)); do
    if curl --fail --silent "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "$label did not become ready at $url."
}

wait_for_redis() {
  local index
  for ((index = 1; index <= 30; index += 1)); do
    if [[ "$(docker compose exec -T redis redis-cli --raw PING 2>/dev/null || true)" == "PONG" ]]; then
      return 0
    fi
    sleep 1
  done
  fail "Redis did not become ready."
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
  [[ -n "$AUTH_TOKEN" ]] || fail "PagerAgent did not issue the local demo session."
}

api_curl() {
  curl --header "Authorization: Bearer $AUTH_TOKEN" "$@"
}

db_value() {
  docker compose exec -T postgres \
    psql --username pageragent --dbname pageragent --tuples-only --no-align \
    --command "$1" | tr -d '[:space:]'
}

assert_equal() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  if [[ "$actual" != "$expected" ]]; then
    fail "$label: expected '$expected', received '$actual'."
  fi
}

workflow_counts() {
  local incident_id="$1"
  db_value "
    SELECT
      (SELECT count(*) FROM investigation_runs WHERE incident_id = '$incident_id')::text
      || '|' ||
      (SELECT count(*) FROM mitigation_proposals WHERE incident_id = '$incident_id')::text
      || '|' ||
      (SELECT count(*)
         FROM mitigation_executions execution
         JOIN mitigation_proposals proposal ON proposal.id = execution.proposal_id
        WHERE proposal.incident_id = '$incident_id')::text;
  "
}

wait_for_workflow_status() {
  local incident_id="$1"
  local workflow_type="$2"
  local expected_status="$3"
  local attempts="${4:-120}"
  local index
  local status
  for ((index = 1; index <= attempts; index += 1)); do
    WORKFLOW_JSON="$(
      api_curl --fail --silent \
        "http://localhost:8000/api/v1/incidents/$incident_id/workflows"
    )"
    status="$(
      printf '%s' "$WORKFLOW_JSON" | python3 -c '
import json
import sys

workflow_type = sys.argv[1]
items = json.load(sys.stdin)
item = next((value for value in items if value["workflow_type"] == workflow_type), None)
print(item["status"] if item else "")
' "$workflow_type"
    )"
    if [[ "$status" == "$expected_status" ]]; then
      return 0
    fi
    if [[ "$status" == "dead_lettered" ]]; then
      printf '%s' "$WORKFLOW_JSON" | python3 -m json.tool >&2
      fail "$workflow_type workflow entered the dead-letter state."
    fi
    sleep 0.5
  done
  fail "$workflow_type workflow did not reach $expected_status."
}

wait_for_proposal_status() {
  local incident_id="$1"
  local expected_status="$2"
  local attempts="${3:-120}"
  local index
  local status
  for ((index = 1; index <= attempts; index += 1)); do
    PROPOSAL_JSON="$(
      api_curl --silent --fail \
        "http://localhost:8000/api/v1/incidents/$incident_id/proposals/latest" \
        2>/dev/null || true
    )"
    if [[ -n "$PROPOSAL_JSON" ]]; then
      status="$(
        printf '%s' "$PROPOSAL_JSON" \
          | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
      )"
      if [[ "$status" == "$expected_status" ]]; then
        return 0
      fi
      if [[ "$status" == "execution_failed" ]]; then
        printf '%s' "$PROPOSAL_JSON" | python3 -m json.tool >&2
        fail "Durable mitigation execution failed."
      fi
    fi
    sleep 0.5
  done
  fail "Proposal did not reach $expected_status."
}

restart_runtime() {
  if [[ "$REDIS_STOPPED" == "true" ]]; then
    docker compose up --detach redis >/dev/null 2>&1 || true
    for _ in {1..30}; do
      if [[ "$(docker compose exec -T redis redis-cli --raw PING 2>/dev/null || true)" == "PONG" ]]; then
        break
      fi
      sleep 1
    done
    REDIS_STOPPED=false
  fi
  if [[ "$ALERT_EVALUATOR_STOPPED" == "true" ]]; then
    docker compose up --detach alert-evaluator >/dev/null 2>&1 || true
    ALERT_EVALUATOR_STOPPED=false
  fi
  if [[ "$WORKFLOW_SERVICES_STOPPED" == "true" ]]; then
    docker compose up --detach outbox-relay workflow-worker >/dev/null 2>&1 || true
    WORKFLOW_SERVICES_STOPPED=false
  fi
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [[ "$STACK_STARTED" == "true" ]]; then
    restart_runtime
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

inject_duplicate_delivery() {
  local incident_id="$1"
  local step_type="$2"
  local job_id
  job_id="$(db_value "
    SELECT job.id
      FROM workflow_jobs job
      JOIN workflow_runs run ON run.id = job.workflow_run_id
     WHERE run.incident_id = '$incident_id'
       AND job.step_type = '$step_type'
     ORDER BY job.created_at
     LIMIT 1;
  ")"
  [[ -n "$job_id" ]] || fail "Could not find completed $step_type workflow job."

  docker compose stop workflow-worker >/dev/null
  WORKFLOW_SERVICES_STOPPED=true
  docker compose exec -T redis redis-cli XADD "$WORKFLOW_STREAM_NAME" '*' \
    workflow_job_id "$job_id" \
    topic workflow.job.ready \
    dispatch_attempt 999 \
    payload '{}' >/dev/null
  docker compose run --rm --no-deps -T workflow-worker \
    python -m app.workflows.worker work --once
  docker compose up --detach workflow-worker >/dev/null
  WORKFLOW_SERVICES_STOPPED=false
}

require_command curl
require_command docker
require_command python3
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example for the local demo."
fi

step "Quiesce any runtime left by an earlier demo"
docker compose stop \
  alert-evaluator outbox-relay workflow-worker >/dev/null 2>&1 || true
ALERT_EVALUATOR_STOPPED=true
WORKFLOW_SERVICES_STOPPED=true
STACK_STARTED=true

step "Start the local PagerAgent core without workflow consumers"
compose_up=(docker compose up --detach)
if [[ "$BUILD_IMAGES" == "true" ]]; then
  compose_up+=(--build)
fi
compose_up+=(
  backend checkout-api redis frontend
)
"${compose_up[@]}"
if [[ "$BUILD_IMAGES" == "true" ]]; then
  docker compose build alert-evaluator outbox-relay workflow-worker
fi
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:8100/health" "Checkout simulator"
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
[[ -n "$WORKFLOW_STREAM_NAME" && -n "$WORKFLOW_DEAD_LETTER_STREAM" ]] \
  || fail "Could not resolve workflow stream names from the runtime configuration."

step "Reset only local demo records, telemetry, and workflow streams"
api_curl --fail --silent --request DELETE \
  http://localhost:8000/api/v1/dev/incidents >/dev/null
curl --fail --silent --request POST \
  http://localhost:8100/admin/reset >/dev/null
docker compose exec -T redis redis-cli DEL \
  "$WORKFLOW_STREAM_NAME" "$WORKFLOW_DEAD_LETTER_STREAM" >/dev/null

docker compose up --detach alert-evaluator >/dev/null
ALERT_EVALUATOR_STOPPED=false

if [[ "$SIMULATE_REDIS_OUTAGE" == "true" ]]; then
  step "Take Redis offline while the API remains available"
  docker compose stop redis >/dev/null
  REDIS_STOPPED=true
  wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API without Redis"
else
  step "Keep Redis available while the relay and worker remain stopped"
fi

step "Generate a deterministic checkout incident with no workflow runtime"
docker compose --profile tools run --rm traffic-generator \
  --requests 20 --delay 0 --run-id durability-healthy
curl --fail --silent --request POST \
  http://localhost:8100/admin/scenarios/checkout-validation-bug/activate >/dev/null
docker compose --profile tools run --rm traffic-generator \
  --requests 40 --delay 0 --run-id durability-outage

incident_id=""
for _ in {1..60}; do
  incidents="$(api_curl --fail --silent http://localhost:8000/api/v1/incidents)"
  incident_id="$(
    printf '%s' "$incidents" \
      | python3 -c 'import json, sys; values=json.load(sys.stdin); print(values[0]["id"] if values else "")'
  )"
  [[ -n "$incident_id" ]] && break
  sleep 0.5
done
[[ -n "$incident_id" ]] || fail "The alert evaluator did not create an incident."
echo "Incident: $incident_id"

step "Prove workflow intent and its unpublished outbox row are in PostgreSQL"
workflow_json="$(
  api_curl --fail --silent \
    "http://localhost:8000/api/v1/incidents/$incident_id/workflows"
)"
queue_summary="$(
  printf '%s' "$workflow_json" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
assert len(items) == 1, items
run = items[0]
assert run["status"] == "queued", run
assert len(run["jobs"]) == 1, run
assert run["jobs"][0]["attempt_count"] == 0, run
print("{}|{}|{}".format(run["id"], run["status"], run["jobs"][0]["status"]))
'
)"
IFS='|' read -r response_workflow_id response_status response_job_status <<<"$queue_summary"
outbox_summary="$(db_value "
  SELECT count(*)::text || '|' ||
         count(*) FILTER (WHERE message.published_at IS NULL)::text
    FROM outbox_messages message
    JOIN workflow_jobs job ON job.id = message.workflow_job_id
    JOIN workflow_runs run ON run.id = job.workflow_run_id
   WHERE run.incident_id = '$incident_id';
")"
assert_equal "$response_status" "queued" "Workflow status while runtime is offline"
assert_equal "$response_job_status" "queued" "Workflow job status while runtime is offline"
assert_equal "$outbox_summary" "1|1" "Committed and unpublished outbox counts"
echo "Workflow: $response_workflow_id (queued)"
echo "Outbox: 1 committed, 1 unpublished"

step "Restart the API and verify the queued workflow survives process loss"
docker compose restart backend >/dev/null
wait_for_url "http://localhost:8000/api/v1/health" "Restarted PagerAgent API"
surviving_workflow_id="$(
  api_curl --fail --silent \
    "http://localhost:8000/api/v1/incidents/$incident_id/workflows" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])'
)"
assert_equal "$surviving_workflow_id" "$response_workflow_id" "Durable workflow identity"
assert_equal "$(db_value "SELECT count(*) FROM outbox_messages WHERE published_at IS NULL;")" \
  "1" "Unpublished outbox count after API restart"

step "Restore Redis and publish the committed outbox intent"
if [[ "$REDIS_STOPPED" == "true" ]]; then
  docker compose up --detach redis >/dev/null
  wait_for_redis
  REDIS_STOPPED=false
fi
docker compose up --detach outbox-relay >/dev/null
published_count="0"
for _ in {1..60}; do
  published_count="$(db_value "
    SELECT count(*)
      FROM outbox_messages message
      JOIN workflow_jobs job ON job.id = message.workflow_job_id
      JOIN workflow_runs run ON run.id = job.workflow_run_id
     WHERE run.incident_id = '$incident_id'
       AND message.published_at IS NOT NULL;
  ")"
  stream_length="$(
    docker compose exec -T redis redis-cli --raw XLEN "$WORKFLOW_STREAM_NAME" \
      2>/dev/null || true
  )"
  if [[ "$published_count" == "1" && "$stream_length" == "1" ]]; then
    break
  fi
  sleep 0.5
done
assert_equal "$published_count" "1" "Published outbox receipt count"
assert_equal "${stream_length:-0}" "1" "Initial Redis stream length"

step "Erase the acknowledged stream and repair it from PostgreSQL"
docker compose stop outbox-relay >/dev/null
docker compose exec -T redis redis-cli DEL \
  "$WORKFLOW_STREAM_NAME" "$WORKFLOW_DEAD_LETTER_STREAM" >/dev/null
sleep 1
docker compose run --rm --no-deps -T \
  -e WORKFLOW_DELIVERY_REPAIR_SECONDS=1 \
  outbox-relay python -m app.workflows.worker relay --once >/dev/null
assert_equal "$(
  docker compose exec -T redis redis-cli --raw XLEN "$WORKFLOW_STREAM_NAME"
)" "1" "Repaired Redis stream length"
assert_equal "$(db_value "
  SELECT message.publish_attempts
    FROM outbox_messages message
    JOIN workflow_jobs job ON job.id = message.workflow_job_id
    JOIN workflow_runs run ON run.id = job.workflow_run_id
   WHERE run.incident_id = '$incident_id';
")" "2" "PostgreSQL-driven delivery count"
echo "The latest nonterminal delivery was republished after transport loss."

step "Resume the worker from the repaired delivery"
docker compose up --detach outbox-relay workflow-worker >/dev/null
WORKFLOW_SERVICES_STOPPED=false
wait_for_workflow_status "$incident_id" "incident_response" "completed"
wait_for_proposal_status "$incident_id" "pending_approval"
printf '%s' "$WORKFLOW_JSON" | python3 -c '
import json
import sys

workflow = next(item for item in json.load(sys.stdin) if item["workflow_type"] == "incident_response")
print("Recovered workflow events:")
for event in workflow["events"]:
    print("  #{:02d} {}".format(event["sequence"], event["event_type"]))
print("Job attempts: {}".format(
    ", ".join("{}={}".format(job["step_type"], job["attempt_count"]) for job in workflow["jobs"])
))
'

step "Inject a duplicate completed-job delivery and prove it is a no-op"
before_counts="$(workflow_counts "$incident_id")"
assert_equal "$before_counts" "1|1|0" "Counts before duplicate investigation delivery"
inject_duplicate_delivery "$incident_id" "investigate"
after_counts="$(workflow_counts "$incident_id")"
assert_equal "$after_counts" "$before_counts" "Counts after duplicate investigation delivery"
echo "investigations|proposals|executions = $after_counts"

if [[ "$AUTO_APPROVE" == "true" ]]; then
  step "Record human approval and let the durable mitigation workflow execute"
  incident_json="$(
    api_curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id"
  )"
  incident_version="$(
    printf '%s' "$incident_json" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
  )"
  transition_body="$(python3 -c '
import json
import sys

print(json.dumps({
    "to_status": "investigating",
    "note": "Reviewed durable evidence before approving the typed rollback.",
    "expected_version": int(sys.argv[1]),
}))
' "$incident_version")"
  api_curl --fail --silent --request POST \
    --header "Content-Type: application/json" \
    --data "$transition_body" \
    "http://localhost:8000/api/v1/incidents/$incident_id/transitions" >/dev/null

  proposal_id="$(
    printf '%s' "$PROPOSAL_JSON" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])'
  )"
  decision_body='{"decision":"approve","note":"Explicit approval after reviewing cited evidence."}'
  approved="$(
    api_curl --fail --silent --request POST \
      --header "Content-Type: application/json" \
      --data "$decision_body" \
      "http://localhost:8000/api/v1/proposals/$proposal_id/decisions"
  )"
  assert_equal "$(printf '%s' "$approved" | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])')" \
    "approved" "Proposal status immediately after durable approval"

  wait_for_workflow_status "$incident_id" "mitigation" "completed"
  wait_for_proposal_status "$incident_id" "verification_passed"
  assert_equal "$(workflow_counts "$incident_id")" "1|1|1" \
    "Counts after durable mitigation"
  assert_equal "$(
    api_curl --fail --silent "http://localhost:8000/api/v1/incidents/$incident_id" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
  )" "mitigated" "Incident status after recovery verification"

  step "Redeliver the completed mitigation job and prove the external write is not repeated"
  deployment_count_before="$(
    curl --fail --silent http://localhost:8100/telemetry \
      | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["deployments"]))'
  )"
  inject_duplicate_delivery "$incident_id" "execute_mitigation"
  assert_equal "$(workflow_counts "$incident_id")" "1|1|1" \
    "Counts after duplicate mitigation delivery"
  deployment_count_after="$(
    curl --fail --silent http://localhost:8100/telemetry \
      | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["deployments"]))'
  )"
  assert_equal "$deployment_count_after" "$deployment_count_before" \
    "Simulator deployment count after duplicate mitigation delivery"
  echo "One approval, one execution receipt, and one rollback mutation are preserved."
else
  step "Stop at the human authority boundary"
  echo "The proposal is pending approval and mitigation executions remain at zero."
  echo "Rerun with --approve to demonstrate the durable mitigation workflow."
fi

step "Durability demonstration passed"
echo "PostgreSQL remained authoritative while Redis and workers were unavailable."
echo "At-least-once delivery produced effectively-once domain effects."
echo "Dashboard: http://localhost:5173"
echo "The local stack and its volumes remain running for inspection."
