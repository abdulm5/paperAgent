#!/usr/bin/env bash

set -euo pipefail

SCENARIO="checkout-validation-bug"
AUTO_APPROVE=false
CHECK_ONLY=false

usage() {
  cat <<'EOF'
Usage: ./scripts/seed-interview-demo.sh [options]

Reset and seed one deterministic PagerAgent interview incident using the existing
end-to-end demo runner. The default stops at the human approval boundary so the
operator can finish the response in the dashboard.

Options:
  --scenario ID   checkout-validation-bug (default), payment-provider-timeout,
                  or checkout-feature-flag-regression
  --approve       For automation-eligible scenarios, exercise approval,
                  verified mitigation, resolution, and postmortem generation
                  from the terminal; upstream failures remain advisory-only
  --check         Validate prerequisites and the Compose configuration only
  -h, --help      Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scenario)
      [[ $# -ge 2 ]] || { echo "--scenario requires an ID" >&2; exit 2; }
      SCENARIO="$2"
      shift 2
      ;;
    --approve)
      AUTO_APPROVE=true
      shift
      ;;
    --check)
      CHECK_ONLY=true
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

case "$SCENARIO" in
  checkout-validation-bug|payment-provider-timeout|checkout-feature-flag-regression) ;;
  *)
    echo "Unsupported interview scenario: $SCENARIO" >&2
    exit 2
    ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "Interview demo preflight failed: $1" >&2
  exit 1
}

for command in docker curl python3; do
  command -v "$command" >/dev/null 2>&1 \
    || fail "required command '$command' is unavailable"
done
docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose is unavailable"
docker info >/dev/null 2>&1 \
  || fail "the Docker daemon is unavailable; start Docker Desktop or your Docker service"
[[ -f .env ]] \
  || fail "missing .env; run 'cp .env.example .env' and retry"
[[ -f "scenarios/$SCENARIO.yaml" ]] \
  || fail "missing scenarios/$SCENARIO.yaml"
[[ -x scripts/run-demo.sh ]] \
  || fail "scripts/run-demo.sh is not executable"

# Keep the recorded interview path independent of a developer's model API key,
# live GitHub credentials, or previously configured Prometheus connectors. The
# full integration demos remain available through the provider-specific scripts.
override_file="$(mktemp "${TMPDIR:-/tmp}/pageragent-interview-compose.XXXXXX")"
cleanup() {
  rm -f "$override_file"
}
trap cleanup EXIT INT TERM

printf '%s\n' \
  'services:' \
  '  backend:' \
  '    environment:' \
  '      PAGERAGENT_ENV: local' \
  '      PAGERAGENT_AUTH_MODE: local' \
  '      PAGERAGENT_SESSION_COOKIE_NAME: pageragent_session' \
  '      PAGERAGENT_SESSION_COOKIE_SECURE: false' \
  '      PAGERAGENT_OIDC_ISSUER: https://identity.pageragent.local' \
  '      PAGERAGENT_INGEST_API_KEY: "${PAGERAGENT_INGEST_API_KEY:-pageragent-local-ingest-key}"' \
  '      PAGERAGENT_INGEST_ORGANIZATION_SLUG: pageragent-labs' \
  '      PAGERAGENT_CONNECTOR_CIPHER_PROVIDER: local' \
  '      PAGERAGENT_CONNECTOR_MASTER_KEY: fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs=' \
  '      PAGERAGENT_CONNECTOR_KEY_VERSION: interview-demo-v1' \
  '      PAGERAGENT_CONNECTOR_DECRYPTION_KEYS: "{}"' \
  '      PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS: http://checkout-api:8100' \
  '      BACKEND_CORS_ORIGINS: http://localhost:5173' \
  '      GITHUB_EVIDENCE_MODE: fixture' \
  '      PROMETHEUS_EVIDENCE_MODE: off' \
  '      SYNTHESIS_PROVIDER: deterministic' \
  '      OPENAI_API_KEY: ""' \
  '      AUTO_INVESTIGATE_INCIDENTS: true' \
  '      AUTO_GENERATE_PROPOSALS: true' \
  '      AUTO_GENERATE_POSTMORTEMS: true' \
  '      DURABLE_MITIGATION_ENABLED: true' \
  '      RECOVERY_CANARY_REQUESTS: 15' \
  '  workflow-worker:' \
  '    environment:' \
  '      PAGERAGENT_ENV: local' \
  '      PAGERAGENT_AUTH_MODE: local' \
  '      PAGERAGENT_CONNECTOR_CIPHER_PROVIDER: local' \
  '      PAGERAGENT_CONNECTOR_MASTER_KEY: fJIrHdOnYNQ6If5g9sz8nNVTsN2I6uav5FRHX24GQMs=' \
  '      PAGERAGENT_CONNECTOR_KEY_VERSION: interview-demo-v1' \
  '      PAGERAGENT_CONNECTOR_DECRYPTION_KEYS: "{}"' \
  '      PAGERAGENT_TELEMETRY_ALLOWED_ORIGINS: http://checkout-api:8100' \
  '      GITHUB_EVIDENCE_MODE: fixture' \
  '      PROMETHEUS_EVIDENCE_MODE: off' \
  '      SYNTHESIS_PROVIDER: deterministic' \
  '      OPENAI_API_KEY: ""' \
  '      AUTO_GENERATE_PROPOSALS: true' \
  '      AUTO_GENERATE_POSTMORTEMS: true' \
  '      DURABLE_MITIGATION_ENABLED: true' \
  '      RECOVERY_CANARY_REQUESTS: 15' \
  >"$override_file"

export COMPOSE_FILE="$ROOT_DIR/docker-compose.yml:$override_file"
export GITHUB_EVIDENCE_MODE=fixture
export PROMETHEUS_EVIDENCE_MODE=off
export ALERT_ERROR_RATE_THRESHOLD=0.05
export ALERT_MINIMUM_REQUESTS=60
export ALERT_WINDOW_SECONDS=300

docker compose config --quiet \
  || fail "the deterministic Compose override is invalid"

python3 - "$SCENARIO" <<'PY'
import sys
from pathlib import Path

scenario_id = sys.argv[1]
text = Path(f"scenarios/{scenario_id}.yaml").read_text()
required = {
    "schema_version: \"1.0\"",
    f"id: {scenario_id}",
    "ground_truth:",
    "expected_action:",
    "thresholds:",
}
missing = sorted(marker for marker in required if marker not in text)
if missing:
    raise SystemExit(f"scenario contract is missing markers: {', '.join(missing)}")
PY

echo "PagerAgent interview demo preflight passed."
echo "  Scenario: $SCENARIO"
echo "  Synthesis: deterministic"
echo "  GitHub evidence: versioned fixture"
echo "  Prometheus evidence: disabled for repeatability"
if [[ "$SCENARIO" == "payment-provider-timeout" ]]; then
  echo "  Approval: unavailable by policy (advisory-only scenario)"
else
  echo "  Approval: $([[ "$AUTO_APPROVE" == "true" ]] && echo terminal || echo dashboard)"
fi

if [[ "$CHECK_ONLY" == "true" ]]; then
  exit 0
fi

arguments=(--scenario "$SCENARIO")
if [[ "$AUTO_APPROVE" == "true" ]]; then
  arguments+=(--approve)
fi

./scripts/run-demo.sh "${arguments[@]}"

if [[ "$SCENARIO" == "payment-provider-timeout" ]]; then
  echo
  echo "Seed complete. Open http://localhost:5173 and sign in as the local"
  echo "incident commander to inspect the red-herring deploy and advisory boundary."
elif [[ "$AUTO_APPROVE" != "true" ]]; then
  echo
  echo "Seed complete. Open http://localhost:5173 and sign in as the local"
  echo "incident commander to review evidence, begin investigation, and approve."
fi
