# Milestone 1: deterministic checkout outage

## What this milestone proves

PagerAgent now receives a real HTTP alert derived from observable service behavior. The expected root cause is known in advance, so later investigation features can be scored against ground truth instead of judged by how convincing they sound.

## Components

1. `checkout-api` accepts checkouts and records structured request telemetry.
2. `traffic-generator` creates a repeatable payment-method mix over HTTP.
3. `alert-evaluator` reads telemetry independently and applies a 5% error-rate threshold.
4. `pageragent-api` validates, deduplicates, and exposes the resulting incident.

The separation matters: checkout does not declare itself broken, and PagerAgent does not inspect simulator internals. The conclusion comes from the same types of signals used in a real monitoring pipeline.

## Reproducible failure

| Release | Commit | Behavior |
| --- | --- | --- |
| `stable-v1` | `2ab1e90` | All supported payment methods succeed. |
| `faulty-v2` | `8fa23c1` | Digital-wallet validation returns `ValidationRuleMissing`. |

Every fifth synthetic request uses a digital wallet. The demo sends 20 healthy requests, deploys `faulty-v2`, and sends 40 more requests. Exactly 8 requests fail, producing a 13.3% error rate over the 60-request monitoring window.

## Evidence produced

Each checkout event contains a timestamp, service, endpoint, request ID, trace ID, payment method, release, commit SHA, HTTP status, latency, and error type. `/telemetry` exposes the structured event window, while `/metrics` exposes Prometheus-compatible aggregates.

The alert sent to PagerAgent carries:

- the observed value, threshold, window, and request counts;
- the first failure and detection timestamps;
- the active release, commit SHA, and deployment timestamp;
- a URL back to the source telemetry.

## Interview explanation

“I started with a deterministic incident generator because an AI investigator needs objective ground truth. I modeled traffic, deployments, telemetry, and alert evaluation as separate responsibilities. That lets me test whether PagerAgent reaches the correct conclusion without leaking the answer from the simulator into the copilot.”

## Boundary of this milestone

PagerAgent stores incidents in memory for visibility and alert deduplication. PostgreSQL persistence, lifecycle transitions, evidence ranking, and the operator dashboard belong to the next milestones.
