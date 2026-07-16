# ADR 0012: Observability evidence is bounded before it is causal

- Status: Accepted
- Date: 2026-07-16

## Context

Prometheus can independently corroborate the metric that created an incident, but its HTTP API is
also a powerful query surface. An arbitrary PromQL expression may expand across high-cardinality
series, consume substantial server CPU and memory, or expose telemetry from services outside the
incident. A successful JSON response is therefore neither a safe evidence contract nor proof of a
cause.

PagerAgent also has a concurrency problem: an administrator can disable a connector, rotate its
bearer token, or change its service binding while an investigation is waiting on the network.
Holding a database row lock throughout provider I/O would prevent that race but make database
availability depend on provider latency.

Finally, “OpenTelemetry evidence” is not one adapter contract. OpenTelemetry standardizes telemetry
signals and transport, while stored log and trace queries are backend-specific. Claiming a generic
OpenTelemetry query adapter would hide the actual authentication, pagination, query, and redaction
boundaries that still need to be designed.

## Decision

Phase 9C.1 implements one Prometheus metrics adapter and defers logs and traces until PagerAgent
selects explicit backend APIs.

PagerAgent applies the following rules:

1. A Prometheus connector binds one organization-owned service to one exact root origin. Only one
   validated connector may be enabled for that organization/service pair.
2. The bearer token remains a write-only Phase 9A envelope credential. The adapter decrypts it only
   at construction and never includes it in evidence, audit events, workflow messages, URLs,
   traces, or provider errors.
3. Validation and collection end their database read transactions before making provider calls.
   They later lock and compare the connector configuration, connector revision, and credential
   revision before recording validation or evidence.
4. Incidents select a query by metric name from a versioned server-owned catalog. They cannot
   supply PromQL, paths, timeouts, steps, series limits, or provider origins.
5. The adapter uses fixed form-encoded `POST` paths, ordinary TLS verification, disabled redirects,
   disabled environment proxies, sanitized status errors, one request per operation, and hard
   timeout and decoded-body limits.
6. Matrix results are accepted only after series, samples, window, timestamps, finite values,
   label count, label names, label values, and total payload dimensions pass explicit bounds.
   Unknown labels and native histograms fail closed in this first contract.
7. The normalized result is stored as a content-hashed `prometheus_metric_snapshot` with a
   sanitized internal source URI and exact connector, credential, provider, and catalog revisions.
8. Prometheus evidence can add only a small deterministic corroboration bonus to an already
   evidence-backed non-unknown cause. It cannot create a causal class or automation authority.

The exact connector origin must be present in server configuration; production requires HTTPS.
Application origin validation, `trust_env=False`, and redirect refusal are necessary but do not pin
DNS to the outbound socket. PagerAgent therefore requires deployment-level egress enforcement that
allows the intended Prometheus destination and denies metadata or unrelated internal destinations.
We explicitly do not describe DNS pre-resolution alone as a production-safe SSRF defense.

These choices follow the Prometheus
[HTTP API contract](https://prometheus.io/docs/prometheus/latest/querying/api/) while accounting for
its [security model](https://prometheus.io/docs/operating/security/), which warns that query access
can expose all time series and overload the server. Returned-series `limit` is retained, but
PagerAgent also counts the normalized response itself and expects production Prometheus operators
to configure server-side query timeout, concurrency, and maximum-sample controls.

## Alternatives considered

### Accept PromQL in the alert or connector configuration

Rejected. Escaping alone would not constrain query cost or tenant reach. A reviewed catalog makes
the allowed query and its evolution visible in source control.

### Reuse the alert's telemetry URL as the Prometheus destination

Rejected. An alert body is evidence, not network authority. The organization/service connector and
server allowlist choose the outbound origin.

### Hold the connector lock during the provider request

Rejected. Slow observability infrastructure would extend database lock time. Snapshot, unlocked
I/O, and a final revision fence preserve revocation semantics without coupling the lock to network
latency.

### Treat a matching error-rate series as a root cause

Rejected. It confirms symptom magnitude, not why the symptom exists. Code, configuration, and
dependency evidence still determine the causal candidate and policy envelope.

### Call the slice a generic OpenTelemetry adapter

Rejected. Logs and traces need named storage backends and separately reviewed query contracts.

## Consequences

- The first query catalog is intentionally narrow: only `http_server_error_rate` is supported.
- Unsupported metrics return no Prometheus evidence in local `auto` mode and fail explicitly in
  `connector` mode. Non-local environments require connector mode.
- Migration 0010 disables every existing Prometheus connector, clears its local-only validation,
  increments its version, and appends a sanitized audit event. Downgrade repeats the fail-closed
  transition instead of silently restoring authority.
- The Docker demo proves the adapter and evidence path, not production network isolation. Hosted
  deployments must add the documented authorization and egress controls.
- Backend-specific logs and traces, richer metric catalog entries, and optional socket-level DNS
  pinning remain explicit follow-on work rather than implicit claims.
