# Phase 9C.1: Bounded Prometheus metric evidence

Phase 9C.1 turns the encrypted Prometheus connector reserved in Phase 9A into a live, tenant-bound
metric source. PagerAgent uses one server-owned PromQL catalog entry to corroborate an alert,
normalizes the response into a content-hashed investigation artifact, and treats that metric as a
small causal confidence adjustment rather than a new source of operational authority.

This first slice is deliberately **Prometheus metrics only**. OpenTelemetry defines telemetry
production and transport conventions, not one universal query API for stored logs and traces.
PagerAgent will add logs and traces only after choosing and securing explicit backend APIs, such as
a trace or log store, in the next 9C slice.

## What this milestone proves

- A Prometheus bearer token remains inside the existing per-revision encrypted connector envelope
  and is never returned through connector, audit, workflow, or evidence APIs.
- Validation performs a credential-bearing, fixed `vector(1)` read outside a database transaction,
  then records the result only if the connector and credential revisions remain current.
- Exactly one enabled Prometheus connector can own an organization/service binding. Competing
  enable operations serialize through the organization row.
- An investigation cannot submit arbitrary PromQL. It selects a versioned server-owned query by
  alert metric and safely supplies only the validated service binding.
- Provider time, response bytes, returned series, samples, labels, label lengths, query window, and
  step are bounded before data reaches the evidence graph.
- A connector disable, configuration edit, or credential rotation completed during collection
  invalidates the stale result before evidence persistence.
- The normalized `prometheus_metric_snapshot` is content-hashed and carries the connector,
  connector revision, credential revision, provider version, and query-catalog version.
- Prometheus can corroborate an existing structured failure signal, but it cannot manufacture a
  cause, override missing evidence, or unlock a mitigation.

## Query contract and hard bounds

The current query catalog contains one entry:

| Alert metric | Catalog query ID | Fixed selector |
| --- | --- | --- |
| `http_server_error_rate` | `alert.http-server-error-rate.v1` | `http_server_error_rate{service="<validated service>"}` |

PagerAgent sends form-encoded `POST /api/v1/query_range` requests to a fixed path. The incident
supplies neither PromQL nor an API path. The service grammar excludes quotes and backslashes, and
the rendered selector is owned by the catalog version in source control. Prometheus documents the
range-query parameters and response matrix in its
[HTTP API reference](https://prometheus.io/docs/prometheus/latest/querying/api/).

Default application limits are:

| Resource | Default | Valid configuration ceiling |
| --- | ---: | ---: |
| Requests per validation or collection | 1 | 1 in this slice |
| HTTP timeout | 6 seconds | 30 seconds |
| Decoded response body | 1 MiB | 4 MiB |
| Returned series | 50 | 100 |
| Returned samples across all series | 2,000 | 10,000 |
| Labels per series | 16 | 32 |
| Query window | 30 minutes | 6 hours |
| Query step | 15 seconds | 15–300 seconds |

Label names are limited to 100 characters, persisted label values to 256 characters, and only
`__name__`, `service`, `job`, `instance`, `cluster`, and `namespace` may cross the normalization
boundary. Native histograms, duplicate JSON keys, duplicate series, non-finite values, unordered or
out-of-window timestamps, unapproved labels, and malformed result types fail closed. The adapter
sorts normalized labels and series before the canonical artifact hash is calculated.

Prometheus's `limit` parameter caps returned series; it is not a complete bound on the work needed
to evaluate a query. The Docker server therefore also sets a query timeout and concurrency limit,
and a production server should set an appropriate `query.max-samples` and resource policy. The
[Prometheus command-line reference](https://prometheus.io/docs/prometheus/latest/command-line/prometheus/)
documents those server controls. The catalog remains narrow because Prometheus explicitly warns
that powerful or high-cardinality queries can exhaust a monitoring server in its
[security model](https://prometheus.io/docs/operating/security/).

## Origin and deployment network boundary

Connector creation accepts only an exact root origin present in the server-controlled
`PAGERAGENT_CONNECTOR_ALLOWED_ORIGINS` list. Paths, queries, fragments, embedded credentials, and
unlisted ports are rejected. Non-local configuration requires HTTPS. Runtime requests append only
the two fixed Prometheus API paths, disable redirects, ignore environment proxy variables, and
retain normal TLS certificate verification.

These controls do **not** claim that DNS pre-resolution makes the outbound connection safe. The
current adapter does not pin a validated DNS answer to the socket, and DNS plus routing remain
deployment concerns. A production deployment must enforce an egress firewall, Kubernetes network
policy, service-mesh policy, or equivalent control that permits only the intended Prometheus
destination and blocks cloud metadata, loopback, link-local, and unrelated private destinations.
An application-layer pinned resolver/transport could be added later, but exact origin allowlisting
alone is not a substitute for that egress boundary.

## Transaction and lock ordering

Validation follows a two-phase compare-and-swap:

```text
tenant-scoped connector snapshot + encrypted credential revision
                  │ decrypt, copy revisions
                  ▼
             end DB transaction
                  │ fixed credential-bearing vector(1) request
                  ▼
lock connector row → compare connector + credential revisions → validation event
```

Investigation collection follows the same no-lock-across-network rule:

```text
incident organization + service
          │ exactly one enabled Prometheus connector
          ▼
snapshot configuration + connector/credential revisions → end DB transaction
          │
          └──► one bounded Prometheus range query
                         │ normalize outside DB lock
                         ▼
other provider/filesystem reads finish
          │ current multi-provider lock order: GitHub, then Prometheus
          ▼
lock exact Prometheus revision → persist all evidence + rankings → one commit
```

The final Prometheus lock is held through the evidence commit. A revocation that commits before
that lock wins and discards the provider result; a later revocation waits until the already
authorized evidence transaction finishes. Evidence collection never takes the organization-row
enablement lock, avoiding an inverted organization/connector lock order.

## Immutable evidence and causal use

The artifact source is a sanitized internal URI:
`prometheus://connector/<connector-id>/<service>`. Its payload contains provider and catalog
versions, query ID, metric and service, exact window, step, series/sample counts, `truncated=false`,
bounded normalized series, and connector plus credential provenance. It contains no bearer token,
raw provider error, arbitrary PromQL, or caller-controlled navigation URL.

For the current `http_server_error_rate` signal, PagerAgent compares the highest bounded
Prometheus sample with the structured telemetry snapshot. When structured telemetry already shows
failures and the Prometheus peak reaches at least 75% of that error rate, each existing non-unknown
candidate receives only a `0.01` corroboration bonus and cites the observability artifact. Unknown
causes receive no bonus. The metric is therefore a deterministic confidence adjustment, not proof of a root
cause and not an authorization input by itself.

## Docker demo

Start the stack, establish the live connector, then run the normal incident:

```bash
docker compose up --build -d
./scripts/run-connector-demo.sh
./scripts/run-demo.sh
```

Docker Compose runs Prometheus on port `9090`, scrapes the checkout simulator every five seconds,
retains one hour locally, and applies a ten-second server query timeout with four-way query
concurrency. The connector demo proves encrypted token custody, a live fixed read request,
validation/enablement, revision fencing, rotation, and revalidation. The incident demo then creates
a `prometheus_metric_snapshot` alongside the structured telemetry and GitHub/fixture evidence.

The local Prometheus server is intentionally a reproducible development component and does not
prove production bearer-token enforcement or production egress isolation. A hosted deployment must
place the configured origin behind the intended HTTPS authorization boundary and the egress policy
described above.

## Interview explanation

Lead with this sentence:

> I treated a metrics query as both an evidence problem and a resource-exhaustion boundary: the
> incident can select only a versioned query ID, every response dimension is capped and normalized,
> and connector authority is compare-and-swapped after network I/O before the immutable artifact is
> committed.

Then show the query catalog, the connector revision receipt, the evidence window and counts, and
the `0.01` confidence adjustment. The important tradeoff is that a live metric independently corroborates
the outage without being allowed to declare a cause or authorize a production write.

## Deferred 9C work

- Select explicit backend APIs for OpenTelemetry-derived traces and logs.
- Add bounded trace/log schemas, pagination, redaction, and cross-signal correlation.
- Add an application transport that pins validated DNS answers if deployment egress policy is not
  the sole network enforcement point.
- Expand the reviewed query catalog and evaluation corpus without exposing arbitrary PromQL.
