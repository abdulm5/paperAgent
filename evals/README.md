# Evaluation harness

Every scenario supplies ground truth so PagerAgent can be measured as an engineering system rather than judged only by a demo.

The `checkout-validation-bug` benchmark now checks:

- faulty commit is ranked first and is in the top three candidates;
- expected runbook is retrieved at rank one;
- impacted request count and affected payment method match simulated truth;
- every derived cluster, commit candidate, and runbook match cites evidence.

The evaluator lives in `backend/app/evaluation/investigations.py`. Its quality gate is
executed by the backend test suite, so ranking or retrieval regressions fail CI instead
of being discovered during the demo. The investigation evaluator remains deterministic
so ranking regressions are isolated from language-model variability.

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
