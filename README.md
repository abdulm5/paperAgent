# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Milestone 0 — repository foundation.** The repository currently provides a runnable API health check, a frontend shell, local infrastructure, and the contracts that will guide the first end-to-end incident. It intentionally does not yet make AI decisions or modify production systems.

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

For local development outside Docker:

```bash
cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

## Development milestones

1. Foundation (current): project structure, local stack, and architectural contracts.
2. Simulator: a checkout service, deterministic bad deploy, synthetic traffic, and alert ingestion.
3. Incident core: persistence and a dashboard that renders an incident timeline.
4. Evidence: telemetry parsing, commit ranking, and runbook retrieval.
5. Copilot: grounded brief generation, approval workflow, and postmortem export.
6. Evaluation: reproducible scenarios, benchmark metrics, and regression gates in CI.

See [the architecture guide](docs/architecture.md) for the system boundary and [the decision records](docs/decisions/) for the rationale behind the design.
