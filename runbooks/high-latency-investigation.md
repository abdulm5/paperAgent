---
id: high-latency-investigation
service: checkout-api
failure_mode: high-latency
owner: payments-platform
---

# Checkout API high latency investigation

## Preconditions

- Confirm the affected endpoint and p95 latency window.
- Compare upstream and downstream service timings.

## Mitigation

1. Isolate the slow dependency or request cohort.
2. Reduce traffic to the degraded path if an approved flag exists.
3. Watch latency and completion rate after mitigation.
