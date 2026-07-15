# Evaluation harness

Every scenario supplies ground truth so PagerAgent can be measured as an engineering system rather than judged only by a demo.

The Phase 6 suite contains three versioned failure classes:

- `checkout-validation-bug`: a real code regression that safely maps to a rollback;
- `payment-provider-timeout`: an upstream failure with a red-herring nearby deploy that must remain advisory-only;
- `checkout-feature-flag-regression`: a runtime configuration regression that maps to one typed flag disable.

The benchmark checks:

- the ground-truth causal kind and reference are ranked first;
- expected runbook reciprocal rank meets the contract threshold;
- impacted request count and affected payment method match simulated truth;
- generated claims cite only allowed evidence;
- the typed action matches ground truth and grants automation only where permitted;
- red-herring deployments, hallucinated citations, missing evidence, and low-confidence actions cannot defeat the safety boundary.

The suite runner lives in `backend/app/evaluation/benchmark.py`, loads contracts through the versioned scenario registry, and is exposed at `GET /api/v1/evaluations/scorecard`. Its aggregate quality gates execute in the backend tests and appear in the dashboard calibration matrix, so causal, retrieval, or safety regressions fail CI instead of being discovered during the demo. Fixture generation remains deterministic so engineering regressions are isolated from language-model variability.

Phase 4 adds `backend/app/evaluation/proposals.py`. Its regression gate checks:

- all four required claim types are present;
- every citation resolves to evidence from the selected investigation;
- the action envelope matches the expected service and known-good release;
- no execution exists without an append-only approval decision;
- recovery telemetry is verified after execution.

The OpenAI adapter is tested with a mocked Responses API transport, while the same
proposal contract is exercised end to end with the deterministic synthesizer. This
keeps CI reproducible without weakening the production integration boundary.

Phase 5 adds `backend/app/evaluation/postmortems.py`. Its gate checks:

- all required narrative sections are non-empty;
- every narrative, learning, prevention, and timeline item has an allowed citation;
- the report timeline covers the exact pre-generation incident-event set;
- root cause and customer impact match scenario ground truth;
- at least three prevention items turn findings into owned follow-through;
- the latest immutable revision matches the visible document version and finalization state.

Generation is exercised through both the deterministic provider and a mocked OpenAI
Structured Outputs transport. API tests cover generation, revision conflicts, finalization
locks, invalid-citation rejection, and Markdown export.
