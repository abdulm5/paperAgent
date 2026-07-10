# Milestone 2: durable incident core

## What this milestone proves

PagerAgent now owns durable incident state rather than holding an alert in process memory. The incident survives an API restart, duplicate alerts attach to the same active record, and human decisions are recorded as ordered timeline events.

## Persistence model

| Table | Responsibility |
| --- | --- |
| `incidents` | Current service, severity, status, timestamps, and optimistic version |
| `alerts` | Immutable copies of every monitoring delivery and its raw evidence |
| `incident_events` | Append-only detections, duplicate deliveries, and operator transitions |

Alembic owns schema changes. Docker Compose runs `alembic upgrade head` before starting the API, and CI verifies the migration against a clean database.

## Lifecycle contract

```text
detected → investigating → mitigated → resolved
```

Each transition requires the version the operator viewed. PostgreSQL locks the selected incident row, the service verifies the version and allowed transition, then updates current state and appends the timeline event in one transaction. A stale operator receives `409 Conflict` and must reload current state.

Resolving an incident clears its active fingerprint. A future alert with the same fingerprint then creates a new incident while the resolved record remains unchanged.

## Operator console

The dashboard polls the incident queue and loads detail on selection. Its evidence tape displays the measured error rate, threshold, failed and total requests, monitoring window, release, and commit. The incident timeline distinguishes monitoring-system events from human decisions.

Operators can add a note and advance only to the next legal state. The API remains the authority; the frontend renders server-returned state and reloads if another operator wins a version race.

## Verified scenario

The checkout scenario created a critical incident from 60 requests with 8 failures and a 13.3% error rate. PagerAgent was stopped and restarted against the same database; the incident returned with the same ID and evidence. A dashboard action then moved it from `detected` version 1 to `investigating` version 2 and appended the operator note.

## Interview explanation

“Phase 1 produced trustworthy incident signals. Phase 2 made them operational: I separated current state from immutable source alerts and append-only events, used transactions and optimistic versions for safe state changes, and built the dashboard directly against those contracts. That gives later AI features durable evidence and an auditable human decision trail.”

## Boundary of this milestone

The system records the evidence supplied by the alert but does not yet collect telemetry independently, rank root-cause hypotheses, or retrieve a runbook. Those capabilities belong to the evidence pipeline milestone.
