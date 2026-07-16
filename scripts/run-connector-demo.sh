#!/usr/bin/env bash

set -euo pipefail

BUILD_IMAGES=true

usage() {
  cat <<'EOF'
Usage: ./scripts/run-connector-demo.sh [--no-build]

Exercise connector custody and the Phase 9C.1 Prometheus handshake:
create disabled, prove API redaction and role/tenant isolation, run the fixed
read-only provider query, enable, rotate back to disabled, validate again, and
print the append-only audit receipt.

Options:
  --no-build    Reuse the current Docker images.
  -h, --help    Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      BUILD_IMAGES=false
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

fail() {
  echo "Connector demo failed: $1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' is unavailable."
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempt
  for ((attempt = 1; attempt <= 60; attempt += 1)); do
    if curl --connect-timeout 2 --max-time 5 --fail --silent "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  fail "$label did not become ready at $url."
}

authenticate() {
  local persona="$1"
  local organization_slug="$2"
  local response
  response="$(
    curl --connect-timeout 3 --max-time 15 --fail --silent --request POST \
      --header "Content-Type: application/json" \
      --data "{\"persona\":\"$persona\",\"organization_slug\":\"$organization_slug\"}" \
      http://localhost:8000/api/v1/auth/dev/session
  )"
  printf '%s' "$response" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

api_request() {
  local token="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  local arguments=(
    --connect-timeout 3
    --max-time 15
    --fail
    --silent
    --request "$method"
    --config /dev/fd/3
  )
  if [[ -n "$body" ]]; then
    arguments+=(--header "Content-Type: application/json" --data-binary @-)
    printf '%s' "$body" \
      | curl "${arguments[@]}" "http://localhost:8000$path" \
        3<<<"header = \"Authorization: Bearer $token\""
    return
  fi
  curl "${arguments[@]}" "http://localhost:8000$path" \
    3<<<"header = \"Authorization: Bearer $token\""
}

response_status() {
  local token="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  local arguments=(
    --connect-timeout 3
    --max-time 15
    --silent
    --output /dev/null
    --write-out "%{http_code}"
    --request "$method"
    --config /dev/fd/3
  )
  if [[ -n "$body" ]]; then
    arguments+=(--header "Content-Type: application/json" --data-binary @-)
    printf '%s' "$body" \
      | curl "${arguments[@]}" "http://localhost:8000$path" \
        3<<<"header = \"Authorization: Bearer $token\""
    return
  fi
  curl "${arguments[@]}" "http://localhost:8000$path" \
    3<<<"header = \"Authorization: Bearer $token\""
}

assert_absent() {
  local value="$1"
  local forbidden="$2"
  local label="$3"
  if [[ "$value" == *"$forbidden"* ]]; then
    fail "$label exposed submitted credential material."
  fi
}

json_field() {
  local document="$1"
  local field="$2"
  printf '%s' "$document" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field"
}

require_command curl
require_command docker
require_command python3
docker compose version >/dev/null 2>&1 || fail "Docker Compose is unavailable."

echo "Starting the PagerAgent API and connector custody dashboard..."
if [[ "$BUILD_IMAGES" == "true" ]]; then
  docker compose up --detach --build backend frontend
else
  docker compose up --detach backend frontend
fi
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:5173" "PagerAgent dashboard"

admin_token="$(authenticate admin pageragent-labs)"
commander_token="$(authenticate incident-commander pageragent-labs)"
responder_token="$(authenticate responder pageragent-labs)"
sandbox_admin_token="$(authenticate admin sandbox-operations)"
[[ -n "$admin_token" ]] || fail "PagerAgent did not issue the admin demo session."

suffix="$(python3 -c 'import time; print(time.time_ns())')"
connector_name="Prometheus custody demo $suffix"
first_token="pageragent-demo-token-$suffix-one"
rotated_token="pageragent-demo-token-$suffix-two"

create_body="$(printf '%s\n%s\n' "$connector_name" "$first_token" | python3 -c '
import json

name = input()
token = input()
print(json.dumps({
    "name": name,
    "provider": "prometheus",
    "configuration": {
        "service": "checkout-api",
        "base_url": "http://prometheus:9090",
    },
    "credentials": {"bearer_token": token},
}))
')"

echo "Creating a safe-by-default Prometheus connector..."
created="$(api_request "$admin_token" POST /api/v1/connectors "$create_body")"
assert_absent "$created" "$first_token" "Create response"
connector_id="$(json_field "$created" id)"
version="$(json_field "$created" version)"
[[ "$(json_field "$created" enabled)" == "False" ]] \
  || fail "A new connector was not disabled."

detail="$(api_request "$commander_token" GET "/api/v1/connectors/$connector_id")"
events="$(api_request "$commander_token" GET "/api/v1/connectors/$connector_id/events")"
assert_absent "$detail$events" "$first_token" "Commander read receipt"
[[ "$(response_status "$responder_token" GET /api/v1/connectors)" == "403" ]] \
  || fail "A responder could read connector metadata."
[[ "$(response_status "$sandbox_admin_token" GET "/api/v1/connectors/$connector_id")" == "404" ]] \
  || fail "A cross-organization connector ID did not return 404."

echo "Validating the live Prometheus read handshake and authenticated vault envelope..."
validated="$(api_request "$admin_token" POST "/api/v1/connectors/$connector_id/validate" \
  "{\"expected_version\":$version}")"
assert_absent "$validated" "$first_token" "Validation response"
version="$(json_field "$validated" version)"

echo "Explicitly enabling the validated connector..."
enabled="$(api_request "$admin_token" PATCH "/api/v1/connectors/$connector_id" \
  "{\"expected_version\":$version,\"enabled\":true}")"
assert_absent "$enabled" "$first_token" "Enable response"
version="$(json_field "$enabled" version)"
[[ "$(json_field "$enabled" enabled)" == "True" ]] \
  || fail "The validated connector did not become enabled."

rotate_body="$(printf '%s\n%s\n' "$version" "$rotated_token" | python3 -c '
import json

version = int(input())
token = input()
print(json.dumps({
    "expected_version": version,
    "credentials": {"bearer_token": token},
}))
')"

echo "Rotating the write-only credential; authority should fail closed..."
rotated="$(api_request "$admin_token" PUT "/api/v1/connectors/$connector_id/credentials" \
  "$rotate_body")"
assert_absent "$rotated" "$first_token" "Rotation response"
assert_absent "$rotated" "$rotated_token" "Rotation response"
version="$(json_field "$rotated" version)"
[[ "$(json_field "$rotated" enabled)" == "False" ]] \
  || fail "Credential rotation did not disable the connector."

validated="$(api_request "$admin_token" POST "/api/v1/connectors/$connector_id/validate" \
  "{\"expected_version\":$version}")"
assert_absent "$validated" "$first_token" "Post-rotation validation response"
assert_absent "$validated" "$rotated_token" "Post-rotation validation response"
version="$(json_field "$validated" version)"
enabled="$(api_request "$admin_token" PATCH "/api/v1/connectors/$connector_id" \
  "{\"expected_version\":$version,\"enabled\":true}")"
assert_absent "$enabled" "$first_token" "Final enable response"
assert_absent "$enabled" "$rotated_token" "Final enable response"
version="$(json_field "$enabled" version)"
[[ "$(json_field "$enabled" enabled)" == "True" ]] \
  || fail "The rotated and revalidated connector did not become enabled."
[[ "$(json_field "$enabled" credential_version)" == "2" ]] \
  || fail "Credential rotation did not advance the credential revision to 2."

stale_body="{\"expected_version\":$((version - 1)),\"enabled\":false}"
[[ "$(response_status "$admin_token" PATCH "/api/v1/connectors/$connector_id" "$stale_body")" == "409" ]] \
  || fail "A stale connector version did not return 409."

events="$(api_request "$admin_token" GET "/api/v1/connectors/$connector_id/events")"
assert_absent "$events" "$first_token" "Audit history"
assert_absent "$events" "$rotated_token" "Audit history"
printf '%s' "$events" | python3 -c '
import json
import sys

events = json.load(sys.stdin)
expected = [
    "connector.created",
    "connector.validation_completed",
    "connector.updated",
    "connector.credentials_updated",
    "connector.validation_completed",
    "connector.updated",
]
if [item["event_type"] for item in events] != expected:
    raise SystemExit("Unexpected connector audit event sequence")
if [item["connector_version"] for item in events] != list(range(1, 7)):
    raise SystemExit("Connector audit versions are not contiguous")
if not all(item["actor"].startswith("user:") for item in events):
    raise SystemExit("Connector audit actor was not server-derived")
'

storage_receipt="$(
  docker compose exec -T postgres \
    psql --username pageragent --dbname pageragent --tuples-only --no-align \
    --command "
      SELECT key_version
          || '|' || credential_version::text
          || '|' || octet_length(ciphertext)::text
          || '|' || octet_length(wrapped_data_key)::text
          || '|' || encode(ciphertext, 'hex')
          || '|' || encode(wrapped_data_key, 'hex')
        FROM connector_credentials
       WHERE connector_id = '$connector_id';
    "
)"
IFS='|' read -r key_version credential_version ciphertext_bytes wrapped_key_bytes ciphertext_hex wrapped_key_hex \
  <<<"$storage_receipt"
printf '%s\n%s\n%s\n' "$rotated_token" "$ciphertext_hex" "$wrapped_key_hex" | python3 -c '
token = input().encode()
ciphertext = bytes.fromhex(input())
wrapped_key = bytes.fromhex(input())
if token in ciphertext or token in wrapped_key:
    raise SystemExit("Database credential columns contain plaintext")
'
[[ "$credential_version" == "2" ]] || fail "Stored credential revision is not 2."

printf '\nConnector custody and Prometheus handshake proof complete\n'
printf '  Connector: %s (%s)\n' "$connector_name" "$connector_id"
printf '  Current state: enabled, connector version %s\n' "$version"
printf '  Credential receipt: revision %s, wrapping key %s\n' \
  "$credential_version" "$key_version"
printf '  Stored bytes: %s ciphertext, %s wrapped data key; plaintext absent\n' \
  "$ciphertext_bytes" "$wrapped_key_bytes"
printf '  RBAC: commander read allowed; responder read denied\n'
printf '  Tenant boundary: sandbox lookup returned 404\n'
printf '  Concurrency boundary: stale mutation returned 409\n'
printf '  Audit events: '
printf '%s' "$events" | python3 -c '
import json
import sys

print(" -> ".join(item["event_type"] for item in json.load(sys.stdin)))
'
printf '  Provider handshake: fixed read-only Prometheus query passed\n'
printf '  Dashboard: http://localhost:5173\n'
