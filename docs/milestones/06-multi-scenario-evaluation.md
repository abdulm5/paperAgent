# Milestone 6: multi-scenario reliability evaluation

## Outcome

PagerAgent now demonstrates generalization instead of memorizing one outage. Three schema-versioned contracts cover a bad deploy, an upstream provider timeout, and a feature-flag regression. Each contract defines simulation parameters, causal ground truth, expected runbook and action, impacted cohorts, adversarial cases, and pass thresholds.

The investigation adds a persisted cross-signal cause layer above commit correlation. Dependency-specific failures rank `payment-gateway` above the deliberately nearby `9c4e2d1` observability deploy. Configuration history ranks `wallet_validation_v2` without inventing a code change. The action policy then grants different authority: rollback, typed flag disable, or advisory-only escalation.

The dashboard presents the suite as a reliability calibration instrument: aggregate gate tracks sit above a scenario matrix showing each top cause, action boundary, and adversarial-probe result. The live incident view separately shows the causal stack and commit dossier, making correlation versus cause easy to explain.

## Reliability boundaries

1. Scenario files validate against a strict `1.0` contract and the suite requires every named scenario.
2. Cause, runbook, impact, affected-attribute, citation, action, automation, and resilience metrics have explicit gates.
3. Unknown citations are rejected rather than silently removed.
4. Missing or low-confidence evidence always maps to `escalate_only`.
5. Upstream causes never unlock writes; feature-flag writes name exactly one evidence-backed flag.
6. The pure benchmark and the persisted investigation/proposal service path are both tested.
7. Postmortems describe the action that actually ran, including configuration changes without a deploy.

## Interview explanation

Start with the red-herring question: “What if an error begins right after a deploy, but the deploy is unrelated?” Show that commit `9c4e2d1` remains visible in the dossier while dependency evidence outranks it. Then explain the separation of concerns: scenario contract supplies truth, deterministic components produce predictions, the benchmark scores those predictions, and the policy derives operational authority from causal class plus confidence.

This is the phase that turns PagerAgent from a polished scripted demo into an evaluated AI system. The strongest design point is not the 100% fixture score; it is that every score has a contract, every write has a boundary, and a future regression becomes a failing test and a visible dashboard gate.

## Demo narration

1. Run `./scripts/run-benchmark.sh` and show all three scenario verdicts.
2. Open the dashboard calibration matrix and point to cause, action, and adversarial columns.
3. Run `./scripts/run-demo.sh --scenario payment-provider-timeout`.
4. Show `payment-gateway` above the nearby deploy and the advisory-only write boundary.
5. Run `./scripts/run-demo.sh --scenario checkout-feature-flag-regression --approve`.
6. Show the typed flag envelope, approval record, recovery receipt, and configuration-aware postmortem.
7. End by explaining how a fourth causal class would require a contract, evidence signal, runbook, policy mapping, and new regression case.
