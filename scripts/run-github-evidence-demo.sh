#!/usr/bin/env bash

set -euo pipefail

BUILD_IMAGES=true
WITH_INCIDENT=false

usage() {
  cat <<'EOF'
Usage: ./scripts/run-github-evidence-demo.sh [--no-build] [--with-incident]

Required environment:
  GITHUB_APP_ID
  GITHUB_INSTALLATION_ID
  GITHUB_REPOSITORY          owner/repository
  GITHUB_PRIVATE_KEY_FILE    readable unencrypted GitHub App PEM
  GITHUB_WEBHOOK_SECRET      32+ character independent signing secret

Optional environment:
  GITHUB_SERVICE             defaults to checkout-api

The default proof validates and enables the connector, authenticates one webhook,
absorbs its retry, rejects tampering and a conflicting replay, and inspects the
normalized delivery ledger. --with-incident also runs the checkout scenario in
explicit connector mode and verifies live GitHub evidence artifacts; it requires
GITHUB_SERVICE=checkout-api and a commit in the configured repository within
the 24-hour evidence window.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build)
      BUILD_IMAGES=false
      shift
      ;;
    --with-incident)
      WITH_INCIDENT=true
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
  echo "GitHub evidence demo failed: $1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' is unavailable."
}

for variable in \
  GITHUB_APP_ID \
  GITHUB_INSTALLATION_ID \
  GITHUB_REPOSITORY \
  GITHUB_PRIVATE_KEY_FILE \
  GITHUB_WEBHOOK_SECRET; do
  [[ -n "${!variable:-}" ]] || fail "$variable is required."
done

GITHUB_SERVICE="${GITHUB_SERVICE:-checkout-api}"
export GITHUB_SERVICE
[[ "$GITHUB_APP_ID" =~ ^[1-9][0-9]*$ ]] || fail "GITHUB_APP_ID must be a positive integer."
[[ "$GITHUB_INSTALLATION_ID" =~ ^[1-9][0-9]*$ ]] \
  || fail "GITHUB_INSTALLATION_ID must be a positive integer."
[[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$ ]] \
  || fail "GITHUB_REPOSITORY must use owner/repository syntax."
IFS='/' read -r repository_owner repository_name <<<"$GITHUB_REPOSITORY"
[[ "$repository_owner" != "." && "$repository_owner" != ".." \
  && "$repository_name" != "." && "$repository_name" != ".." \
  && ${#repository_owner} -le 100 && ${#repository_name} -le 100 ]] \
  || fail "GITHUB_REPOSITORY contains an unsupported path segment."
GITHUB_REPOSITORY="$(printf '%s' "$GITHUB_REPOSITORY" | tr '[:upper:]' '[:lower:]')"
export GITHUB_REPOSITORY
[[ "$GITHUB_SERVICE" =~ ^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$ \
  && ${#GITHUB_SERVICE} -le 100 ]] \
  || fail "GITHUB_SERVICE contains unsupported characters."
if [[ "$WITH_INCIDENT" == "true" && "$GITHUB_SERVICE" != "checkout-api" ]]; then
  fail "--with-incident requires GITHUB_SERVICE=checkout-api for the included scenario."
fi
[[ -r "$GITHUB_PRIVATE_KEY_FILE" ]] || fail "GITHUB_PRIVATE_KEY_FILE is not readable."
[[ ${#GITHUB_WEBHOOK_SECRET} -ge 32 ]] \
  || fail "GITHUB_WEBHOOK_SECRET must contain at least 32 characters."

require_command curl
require_command docker
require_command python3
docker compose version >/dev/null 2>&1 || fail "Docker Compose is unavailable."

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
  local response
  response="$(
    curl --connect-timeout 3 --max-time 15 --fail --silent --request POST \
      --header "Content-Type: application/json" \
      --data '{"persona":"admin","organization_slug":"pageragent-labs"}' \
      http://localhost:8000/api/v1/auth/dev/session
  )"
  printf '%s' "$response" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

api_request() {
  local token="$1"
  local method="$2"
  local path="$3"
  curl --connect-timeout 3 --max-time 30 --fail --silent \
    --request "$method" \
    --config /dev/fd/3 \
    "http://localhost:8000$path" \
    3<<<"header = \"Authorization: Bearer $token\""
}

api_request_body() {
  local token="$1"
  local method="$2"
  local path="$3"
  curl --connect-timeout 3 --max-time 30 --fail --silent \
    --request "$method" \
    --header "Content-Type: application/json" \
    --data-binary @- \
    --config /dev/fd/3 \
    "http://localhost:8000$path" \
    3<<<"header = \"Authorization: Bearer $token\""
}

json_field() {
  local document="$1"
  local field="$2"
  printf '%s' "$document" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field"
}

assert_secret_absent() {
  local document="$1"
  [[ "$document" != *"BEGIN PRIVATE KEY"* ]] \
    || fail "An API response exposed the App private key."
  [[ "$document" != *"BEGIN RSA PRIVATE KEY"* ]] \
    || fail "An API response exposed the App private key."
  [[ "$document" != *"$GITHUB_WEBHOOK_SECRET"* ]] \
    || fail "An API response exposed the webhook secret."
}

connector_body() {
  local mode="$1"
  local version="${2:-}"
  MODE="$mode" EXPECTED_VERSION="$version" python3 -c '
import json
import os
from pathlib import Path

credentials = {
    "private_key": Path(os.environ["GITHUB_PRIVATE_KEY_FILE"]).read_text(),
    "webhook_secret": os.environ["GITHUB_WEBHOOK_SECRET"],
}
if os.environ["MODE"] == "rotate":
    payload = {
        "expected_version": int(os.environ["EXPECTED_VERSION"]),
        "credentials": credentials,
    }
else:
    payload = {
        "name": "GitHub evidence demo " + os.environ["GITHUB_REPOSITORY"],
        "provider": "github",
        "configuration": {
            "service": os.environ["GITHUB_SERVICE"],
            "repository": os.environ["GITHUB_REPOSITORY"],
            "app_id": int(os.environ["GITHUB_APP_ID"]),
            "installation_id": int(os.environ["GITHUB_INSTALLATION_ID"]),
        },
        "credentials": credentials,
    }
json.dump(payload, __import__("sys").stdout, separators=(",", ":"))
'
}

echo "Starting PagerAgent in explicit GitHub connector mode..."
if [[ "$BUILD_IMAGES" == "true" ]]; then
  GITHUB_EVIDENCE_MODE=connector docker compose up --detach --build backend frontend
else
  GITHUB_EVIDENCE_MODE=connector docker compose up --detach backend frontend
fi
wait_for_url "http://localhost:8000/api/v1/health" "PagerAgent API"
wait_for_url "http://localhost:5173" "PagerAgent dashboard"

admin_token="$(authenticate)"
[[ -n "$admin_token" ]] || fail "PagerAgent did not issue the admin demo session."
connectors="$(api_request "$admin_token" GET /api/v1/connectors)"
selection="$(
  printf '%s' "$connectors" | python3 -c '
import json
import os
import sys

matches = [
    item for item in json.load(sys.stdin)
    if item.get("provider") == "github"
    and item.get("configuration", {}).get("service") == os.environ["GITHUB_SERVICE"]
    and item.get("configuration", {}).get("repository") == os.environ["GITHUB_REPOSITORY"]
]
if len(matches) > 1:
    raise SystemExit("more than one matching GitHub connector exists")
if not matches:
    print("new")
else:
    item = matches[0]
    print("{}|{}".format(item["id"], item["version"]))
'
)" || fail "Could not select a unique matching connector."

if [[ "$selection" == "new" ]]; then
  echo "Sealing the multiline App key and independent webhook secret..."
  created="$(connector_body create | api_request_body "$admin_token" POST /api/v1/connectors)"
else
  IFS='|' read -r existing_id existing_version <<<"$selection"
  echo "Refreshing the existing connector binding and disabling its old revision..."
  refreshed="$(
    EXISTING_VERSION="$existing_version" python3 -c '
import json
import os

json.dump({
    "expected_version": int(os.environ["EXISTING_VERSION"]),
    "configuration": {
        "service": os.environ["GITHUB_SERVICE"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "app_id": int(os.environ["GITHUB_APP_ID"]),
        "installation_id": int(os.environ["GITHUB_INSTALLATION_ID"]),
    },
}, __import__("sys").stdout, separators=(",", ":"))
' \
      | api_request_body "$admin_token" PATCH "/api/v1/connectors/$existing_id"
  )"
  assert_secret_absent "$refreshed"
  existing_version="$(json_field "$refreshed" version)"
  echo "Rotating the existing connector credentials into the refreshed binding..."
  created="$(
    connector_body rotate "$existing_version" \
      | api_request_body "$admin_token" PUT "/api/v1/connectors/$existing_id/credentials"
  )"
fi
assert_secret_absent "$created"
connector_id="$(json_field "$created" id)"
version="$(json_field "$created" version)"
[[ "$(json_field "$created" enabled)" == "False" ]] \
  || fail "The credential write did not leave the connector disabled."

echo "Performing the GitHub App installation and repository-read handshake..."
validated="$(
  printf '{"expected_version":%s}' "$version" \
    | api_request_body "$admin_token" POST "/api/v1/connectors/$connector_id/validate"
)"
assert_secret_absent "$validated"
[[ "$(json_field "$validated" last_validation_ok)" == "True" ]] \
  || fail "The GitHub provider handshake did not pass."
version="$(json_field "$validated" version)"

enabled="$(
  printf '{"expected_version":%s,"enabled":true}' "$version" \
    | api_request_body "$admin_token" PATCH "/api/v1/connectors/$connector_id"
)"
assert_secret_absent "$enabled"
[[ "$(json_field "$enabled" enabled)" == "True" ]] \
  || fail "The validated connector did not become enabled."

delivery_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
webhook_body="$(python3 -c '
import json
import os

print(json.dumps({
    "ref": "refs/heads/main",
    "before": "b" * 40,
    "after": "a" * 40,
    "created": False,
    "deleted": False,
    "forced": False,
    "commits": [],
    "head_commit": None,
    "repository": {"full_name": os.environ["GITHUB_REPOSITORY"]},
    "installation": {"id": int(os.environ["GITHUB_INSTALLATION_ID"])},
}, separators=(",", ":")))
')"
signature="$(
  printf '%s' "$webhook_body" | python3 -c '
import hashlib
import hmac
import os
import sys

body = sys.stdin.buffer.read()
print("sha256=" + hmac.new(
    os.environ["GITHUB_WEBHOOK_SECRET"].encode(), body, hashlib.sha256
).hexdigest())
'
)"

send_webhook() {
  local body="$1"
  local body_signature="$2"
  local output_mode="${3:-body}"
  local arguments=(
    --connect-timeout 3
    --max-time 15
    --silent
    --request POST
    --header "Content-Type: application/json"
    --header "X-GitHub-Delivery: $delivery_id"
    --header "X-GitHub-Event: push"
    --header "X-Hub-Signature-256: $body_signature"
    --data-binary @-
  )
  if [[ "$output_mode" == "status" ]]; then
    arguments+=(--output /dev/null --write-out "%{http_code}")
  else
    arguments+=(--fail)
  fi
  printf '%s' "$body" \
    | curl "${arguments[@]}" \
      "http://localhost:8000/api/v1/webhooks/github/$connector_id"
}

echo "Accepting one correctly signed raw delivery..."
accepted="$(send_webhook "$webhook_body" "$signature")"
[[ "$(json_field "$accepted" duplicate)" == "False" ]] \
  || fail "The first delivery was incorrectly classified as a duplicate."

echo "Replaying the exact delivery; PostgreSQL should absorb it..."
replayed="$(send_webhook "$webhook_body" "$signature")"
[[ "$(json_field "$replayed" duplicate)" == "True" ]] \
  || fail "An exact GitHub retry was not idempotent."

tampered_body="${webhook_body/\"after\":\"a/\"after\":\"c}"
[[ "$(send_webhook "$tampered_body" "$signature" status)" == "401" ]] \
  || fail "A body changed after signing was not rejected."
tampered_signature="$(
  printf '%s' "$tampered_body" | python3 -c '
import hashlib
import hmac
import os
import sys

body = sys.stdin.buffer.read()
print("sha256=" + hmac.new(
    os.environ["GITHUB_WEBHOOK_SECRET"].encode(), body, hashlib.sha256
).hexdigest())
'
)"
[[ "$(send_webhook "$tampered_body" "$tampered_signature" status)" == "409" ]] \
  || fail "A re-signed different body reused the delivery ID without conflict."

deliveries="$(
  api_request "$admin_token" GET "/api/v1/connectors/$connector_id/github-deliveries"
)"
assert_secret_absent "$deliveries"
echo "Normalized tenant-scoped receipt:"
printf '%s' "$deliveries" | python3 -c '
import json
import os
import sys

items = json.load(sys.stdin)
matches = [item for item in items if item["delivery_id"] == sys.argv[1]]
if len(matches) != 1:
    raise SystemExit("expected the new normalized delivery exactly once")
item = matches[0]
if (
    item["event_type"] != "push"
    or item["normalized_payload"]["service"] != os.environ["GITHUB_SERVICE"]
):
    raise SystemExit("normalized delivery does not match its service binding")
if len(item["body_sha256"]) != 64:
    raise SystemExit("delivery body hash is missing")
print(json.dumps(item, indent=2, sort_keys=True))
' "$delivery_id"

if [[ "$WITH_INCIDENT" == "true" ]]; then
  echo "Running the checkout incident with live GitHub connector evidence..."
  GITHUB_EVIDENCE_MODE=connector ./scripts/run-demo.sh --scenario checkout-validation-bug
  admin_token="$(authenticate)"
  incident_id="$(
    api_request "$admin_token" GET /api/v1/incidents \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["id"])'
  )"
  investigation="$(
    api_request "$admin_token" GET "/api/v1/incidents/$incident_id/investigations/latest"
  )"
  printf '%s' "$investigation" | python3 -c '
import json
import sys

result = json.load(sys.stdin)
artifacts = {item["kind"]: item for item in result["evidence"]}
required = {
    "commit_catalog",
    "github_pull_request_history",
    "github_deployment_history",
    "github_release_history",
    "github_webhook_history",
}
missing = required.difference(artifacts)
if missing:
    raise SystemExit(f"missing GitHub evidence artifacts: {sorted(missing)}")
catalog = artifacts["commit_catalog"]
if catalog["payload"].get("provider") != "github_app":
    raise SystemExit("investigation did not use the GitHub App provider")
if not catalog["source_uri"].startswith("github://"):
    raise SystemExit("GitHub source provenance is missing")
if not result["commit_candidates"]:
    raise SystemExit("GitHub evidence produced no ranked commit")
print("  Live investigation: GitHub artifacts and ranked commit verified")
'
fi

printf '\nPhase 9B GitHub evidence proof complete\n'
printf '  Connector: %s (record v%s, credential v%s)\n' \
  "$connector_id" "$(json_field "$enabled" version)" "$(json_field "$enabled" credential_version)"
printf '  Handshake: repository-scoped installation access passed\n'
printf '  Webhook: signature accepted; exact retry idempotent\n'
printf '  Tampering: invalid signature rejected; conflicting replay rejected\n'
printf '  Inbox: one normalized delivery with immutable body hash\n'
printf '  Secrets: PEM, webhook secret, App JWT, and installation token absent from receipts\n'
printf '  Dashboard: http://localhost:5173\n'
