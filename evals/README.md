# Evaluation harness

Every scenario supplies ground truth so PagerAgent can be measured as an engineering system rather than judged only by a demo.

The `checkout-validation-bug` benchmark now checks:

- faulty commit is ranked first and is in the top three candidates;
- expected runbook is retrieved at rank one;
- impacted request count and affected payment method match simulated truth;
- every derived cluster, commit candidate, and runbook match cites evidence.

The evaluator lives in `backend/app/evaluation/investigations.py`. Its quality gate is
executed by the backend test suite, so ranking or retrieval regressions fail CI instead
of being discovered during the demo. The evaluator is intentionally deterministic; LLM
quality evaluation starts when grounded synthesis is introduced in the next milestone.
The investigation layer has no production-action capability; later mitigation execution
will require the human-approval domain event defined in ADR 0002.
