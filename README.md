# PagerAgent

PagerAgent is an evidence-grounded incident-response copilot. It helps an on-call engineer connect an alert to telemetry, deploy history, relevant runbooks, customer impact, and a human-approved mitigation.

## Project status

**Phase 5 — grounded postmortems and incident learning.** PagerAgent now carries a simulated outage from alert through evidence, human-approved recovery, resolution, and a versioned postmortem. The report preserves exact timeline events and citations, supports audited operator revisions, locks after explicit finalization, and exports as Markdown with owned prevention work.

## The interview story

PagerAgent is designed around a simple principle: the model synthesizes evidence; deterministic tools collect and score it. The first complete flow will be:

1. A simulated checkout deployment introduces a validation bug.
2. Telemetry crosses an alert threshold and creates an incident.
3. PagerAgent gathers evidence, ranks deploy candidates, and retrieves a rollback runbook.
4. It proposes a mitigation with confidence and citations.
5. A human operator approves or rejects the proposal.
6. The incident commander resolves the incident after verified recovery.
7. PagerAgent generates a cited postmortem that the team can revise, finalize, and export.

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

The script sends 20 healthy requests, activates `faulty-v2`, sends 40 additional requests, and waits for the alert, investigation, and grounded decision packet. The expected result is one `ValidationRuleMissing` cluster with 8 failed digital-wallet requests, commit `8fa23c1` ranked first, `checkout-api-rollback` retrieved first, and a cited rollback proposal waiting for human approval.

To exercise the explicit command-line approval path as well:

```bash
./scripts/run-demo.sh --approve
```

That flag represents the operator decision. PagerAgent records it, rolls back only to the allow-listed `stable-v1` release, sends 15 recovery canaries including the failing cohort, and marks the incident mitigated only if every canary succeeds. The script then resolves the incident, waits for the automatic grounded postmortem, verifies its Markdown export, and prints the temporary export path. The normal dashboard path keeps approval, report editing, and finalization interactive.

The demo works without an API key using a deterministic grounded synthesizer. To exercise real model synthesis, set `OPENAI_API_KEY` and leave `SYNTHESIS_PROVIDER=auto`; PagerAgent uses the OpenAI Responses API with a strict JSON schema. Set `SYNTHESIS_PROVIDER=deterministic` for reproducible offline demos.

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
4. Copilot (complete): structured grounded briefs, citation guardrails, human decisions, allow-listed rollback execution, and recovery verification.
5. Postmortem (complete): grounded generation after resolution, exact timelines, immutable revisions, explicit finalization, prevention ownership, and Markdown export.
6. Evaluation expansion (next): add scenarios, failure injection, and broader model-quality regression gates.

See [the architecture guide](docs/architecture.md), [milestone walkthroughs](docs/milestones/), and [decision records](docs/decisions/) for the rationale behind the design.
