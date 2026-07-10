# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Milestone 2 — durable incident core.** PagerAgent now persists alerts and incidents in PostgreSQL, records lifecycle changes in an append-only timeline, and presents real evidence and human-operated transitions in the incident command dashboard. AI investigation intentionally remains the next milestone.

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

The script sends 20 healthy requests, activates `faulty-v2`, sends 40 additional requests, and waits for PagerAgent to receive the threshold alert. The expected result is 8 failed digital-wallet requests and a 13.3% error rate over the complete 60-request window. Open <http://localhost:5173> afterward to inspect the evidence and advance the incident lifecycle.

For local development outside Docker:

```bash
cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && alembic upgrade head && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

## Development milestones

1. Foundation (complete): project structure, local stack, and architectural contracts.
2. Simulator (complete): a checkout service, deterministic bad deploy, synthetic traffic, and alert ingestion.
3. Incident core (complete): PostgreSQL persistence, lifecycle rules, and an operator dashboard.
4. Evidence (next): telemetry parsing, commit ranking, and runbook retrieval.
5. Copilot: grounded brief generation, approval workflow, and postmortem export.
6. Evaluation: reproducible scenarios, benchmark metrics, and regression gates in CI.

See [the architecture guide](docs/architecture.md), [milestone walkthroughs](docs/milestones/), and [decision records](docs/decisions/) for the rationale behind the design.
