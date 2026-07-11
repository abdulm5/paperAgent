# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Phase 3 — evidence investigation pipeline.** PagerAgent now turns an alert into an immutable evidence ledger, clusters its failures, ranks suspect commits with explainable feature scores, and retrieves the most relevant runbook. Every derived result carries citations and the deterministic scenario benchmark runs in CI.

## The interview story

PagerAgent is designed around a simple principle: the model synthesizes evidence; deterministic tools collect and score it. The first complete flow will be:

1. A simulated checkout deployment introduces a validation bug.
2. Telemetry crosses an alert threshold and creates an incident.
3. PagerAgent gathers evidence, ranks deploy candidates, and retrieves a rollback runbook.
4. It proposes a mitigation with confidence and citations.
5. A human operator approves or rejects the proposal.
6. The system records a timeline and generates a postmortem from that record.

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend/` | FastAPI application and incident-orchestration APIs |
| `frontend/` | React operator dashboard |
| `simulator/` | Deterministic services, traffic, deploys, and alerts |
| `scenarios/` | Ground-truth incident definitions used by the simulator and evaluations |
| `runbooks/` | Versioned Markdown operational procedures |
| `evals/` | Benchmark definitions and quality gates |
| `docs/` | Architecture notes and explicit engineering decisions |
| `infra/` | Local observability and infrastructure configuration |

## Run locally

Copy the example environment file if needed, then start the development stack:

```bash
cp .env.example .env
docker compose up --build
```

Once running:

- API health: <http://localhost:8000/api/v1/health>
- API documentation: <http://localhost:8000/docs>
- Dashboard: <http://localhost:5173>
- Simulated checkout API: <http://localhost:8100/docs>

To replay the first incident automatically:

```bash
./scripts/run-demo.sh
```

The script sends 20 healthy requests, activates `faulty-v2`, sends 40 additional requests, and waits for the alert and its investigation. The expected result is one `ValidationRuleMissing` cluster with 8 failed digital-wallet requests, commit `8fa23c1` ranked first, and `checkout-api-rollback` retrieved first. Open <http://localhost:5173> afterward to inspect hashes, citations, scoring features, and the incident lifecycle.

For local development outside Docker:

```bash
cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && alembic upgrade head && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

## Development milestones

0. Foundation (complete): project structure, local stack, and architectural contracts.
1. Simulator (complete): a checkout service, deterministic bad deploy, synthetic traffic, and alert ingestion.
2. Incident core (complete): PostgreSQL persistence, lifecycle rules, and an operator dashboard.
3. Evidence (complete): immutable collection, error clustering, explainable commit ranking, hybrid runbook retrieval, and deterministic quality gates.
4. Copilot (next): grounded brief generation, approval workflow, and postmortem export.
5. Evaluation expansion: add scenarios and model-quality regression gates.

See [the architecture guide](docs/architecture.md), [milestone walkthroughs](docs/milestones/), and [decision records](docs/decisions/) for the rationale behind the design.
