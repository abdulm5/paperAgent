#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

docker compose up --detach --build backend
for _ in {1..30}; do
  if curl --fail --silent http://localhost:8000/api/v1/health >/dev/null; then
    break
  fi
  sleep 1
done

scorecard="$(curl --fail --silent http://localhost:8000/api/v1/evaluations/scorecard)"
printf '%s' "$scorecard" | python3 -c '
import json
import sys

scorecard = json.load(sys.stdin)
print("PagerAgent reliability calibration")
print("  Suite: {} / schema {}".format(scorecard["suite_version"], scorecard["schema_version"]))
print("  Verdict: {} ({} scenarios)".format("PASS" if scorecard["passed"] else "FAIL", scorecard["scenario_count"]))
for scenario in scorecard["scenarios"]:
    cause = scenario["predicted_cause"]
    action = scenario["predicted_action"]
    probes = scenario["adversarial_probes"]
    print("  [{}] {}".format("PASS" if scenario["passed"] else "FAIL", scenario["scenario_id"]))
    print("         cause={} / {} action={} probes={}/{}".format(
        cause["kind"],
        cause["reference"],
        action["action_type"],
        sum(probe["passed"] for probe in probes),
        len(probes),
    ))
if not scorecard["passed"]:
    raise SystemExit(1)
'
