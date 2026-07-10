# Deterministic outage simulator

The simulator behaves like a tiny production system that PagerAgent can observe from the outside. It owns checkout behavior and telemetry, while separate traffic and monitoring processes interact with it over HTTP.

## Components

- `app.main`: checkout API, release controls, structured telemetry, and Prometheus metrics.
- `app.traffic`: deterministic synthetic customers. Every fifth request uses a digital wallet.
- `app.monitor`: threshold evaluator that polls checkout telemetry and sends alerts to PagerAgent.

The `faulty-v2` release represents commit `8fa23c1`. It raises `ValidationRuleMissing` for digital-wallet requests, producing a reproducible 20% failure cohort in outage-only traffic.

These controls exist only in the simulator:

```bash
curl -X POST http://localhost:8100/admin/releases/faulty-v2/activate
curl -X POST http://localhost:8100/admin/reset
curl http://localhost:8100/telemetry
curl http://localhost:8100/metrics
```
