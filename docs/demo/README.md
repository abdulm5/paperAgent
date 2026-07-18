# PagerAgent final demo kit

This directory is the presentation layer for the implemented PagerAgent system. It does not add a
second fictional architecture: every command, screen, state transition, and reliability claim here
maps to code in this repository.

## Recommended path

Use the deterministic interview seed when recording or rehearsing:

```bash
cp .env.example .env  # only when .env does not already exist
./scripts/seed-interview-demo.sh --check
./scripts/seed-interview-demo.sh
```

The helper resets the local incident and simulator state, forces deterministic synthesis and Git
fixtures, runs the `checkout-validation-bug` scenario, and stops at the human approval boundary.
It intentionally disables optional live Prometheus evidence so a stale connector cannot make the
core recording nondeterministic. Open <http://localhost:5173>, sign in as the local incident
commander, and finish the response interactively.

For a terminal-only end-to-end verification, including approval, recovery canaries, resolution,
postmortem generation, and Markdown export, run:

```bash
./scripts/seed-interview-demo.sh --approve
```

The provider-specific demonstrations remain separate:

```bash
./scripts/run-connector-demo.sh        # live local Prometheus connector and custody proof
./scripts/run-durability-demo.sh       # Redis/worker outage and replay proof
./scripts/run-github-evidence-demo.sh  # requires a real GitHub App installation
```

## What to read

| Artifact | Use it for |
| --- | --- |
| [video-script.md](video-script.md) | A timed, screen-by-screen recording plan with exact narration |
| [architecture-walkthrough.md](architecture-walkthrough.md) | The end-to-end data flow, trust boundaries, failure semantics, and code map |
| [interview-guide.md](interview-guide.md) | Short explanations and likely interviewer follow-ups |
| [resume-bullets.md](resume-bullets.md) | Accurate résumé bullet options and claims to avoid |

## Canonical deterministic result

The default seed uses the checked-in `checkout-validation-bug` contract:

- 20 healthy requests followed by 40 outage requests;
- 8 `ValidationRuleMissing` failures in the digital-wallet cohort;
- a 13.3% observed error rate against the local 5% threshold;
- commit `8fa23c1` ranked as the top causal signal;
- `checkout-api-rollback` ranked as the rollback runbook;
- one typed `rollback_service` action from `faulty-v2` to `stable-v1`;
- explicit incident-commander approval before execution;
- 15 recovery canaries and zero failures before mitigation is recorded; and
- a cited, versioned postmortem after resolution.

Those numbers belong to a reproducible fixture, not production performance or customer-impact
claims.

## Recording checklist

- Use a fresh browser window and 125–150% terminal zoom.
- Keep the dashboard and terminal side by side; hide environment files and provider credentials.
- Let workflow state changes appear instead of cutting directly to the final result.
- Say “effectively once at the domain boundary,” never “exactly once.”
- Say “deterministic three-scenario evaluation suite,” not “production accuracy.”
- Say “production-capable boundaries” for OIDC/KMS and explain that the stock demo uses local
  personas, a local AES-GCM key, and fixture Git evidence.
- End with one honest limitation: backend-specific log/trace evidence adapters remain deferred.

## Fast verification before recording

```bash
docker compose config --quiet
(cd backend && .venv/bin/ruff check . && .venv/bin/pytest -q)
(cd simulator && .venv/bin/ruff check app tests && .venv/bin/pytest -q)
(cd frontend && npm test -- --run && npm run build)
```

These commands use the per-project virtual environments created by the local-development setup in
the root README. The Compose-based demo itself needs Docker, Docker Compose, `curl`, and Python 3;
`seed-interview-demo.sh --check` also verifies that the Docker daemon is actually reachable before
a recording begins.
