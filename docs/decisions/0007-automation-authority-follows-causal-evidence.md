# ADR 0007: Automation authority follows causal evidence

## Status

Accepted.

## Context

Deploy proximity is useful evidence, but it is not proof. An upstream timeout can begin near an unrelated release, and a runtime feature flag can change behavior without any application deployment. Mapping every incident to a rollback would turn correlation into an unsafe production write.

## Decision

PagerAgent ranks a typed causal signal across code changes, configuration changes, upstream dependencies, and unknown causes. A deterministic policy—not generated prose—maps the top evidence-backed cause to an action envelope:

- a high-confidence code change may propose `rollback_service` to the known-good release;
- a high-confidence configuration change may propose `disable_feature_flag` for exactly the ranked flag;
- an upstream dependency, unknown cause, missing evidence, or score below the confidence floor produces `escalate_only` with automation disabled.

The matching safety runbook must rank first before a proposal is stored. Executable actions still require append-only human approval and executor allow-list checks. The evaluation suite includes red-herring deploy, invented-citation, missing-evidence, and low-confidence probes to prevent regressions in this boundary.

## Consequences

The system can explain why two visually similar incidents receive different operational authority. A false deploy correlation cannot unlock rollback, while a narrowly evidenced configuration change can be mitigated without redeploying code. The policy is intentionally conservative: new causal kinds remain advisory until an explicit action contract, runbook, executor policy, and regression scenario are added.
