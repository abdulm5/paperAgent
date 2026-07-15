---
id: payment-provider-degradation
service: checkout-api
failure_mode: upstream-timeouts
owner: payments-platform
---

# Payment provider degradation

## Preconditions

- Confirm timeout spans terminate at the payment gateway boundary.
- Treat nearby application deploys as unproven until request-path evidence connects them.
- Check provider status and escalation channels.

## Mitigation

1. Escalate to the payment provider owner with cited traces and the affected cohort.
2. Reduce or pause traffic to the degraded payment path only through an approved control.
3. Do not roll back a healthy application release without causal evidence.
4. Verify provider latency and checkout completion before resolving the incident.
