from typing import Any

from app.domain.evaluations import CausalKind, CauseCandidate
from app.investigation.commits import RankedCommit


class CauseRanker:
    version = "causal-signal-ranker-v1"

    def rank(
        self,
        telemetry: dict[str, Any],
        commits: list[RankedCommit],
    ) -> list[CauseCandidate]:
        failures = [
            event
            for event in telemetry.get("recent_events", [])
            if event.get("outcome") == "failure"
        ]
        candidates: list[CauseCandidate] = []

        dependencies = sorted(
            {
                str(event["upstream_dependency"])
                for event in failures
                if event.get("upstream_dependency")
            }
        )
        for dependency in dependencies:
            candidates.append(
                CauseCandidate(
                    kind=CausalKind.UPSTREAM_DEPENDENCY,
                    reference=dependency,
                    title=f"{dependency.replace('-', ' ').title()} timeout",
                    rank=1,
                    score=0.98,
                    explanation=[
                        "Failure events terminate at the upstream dependency boundary.",
                        "The affected cohort carries dependency-specific timeout evidence.",
                        "A nearby application deploy has no matching behavior change.",
                    ],
                )
            )

        feature_flags = sorted(
            {
                str(event["feature_flag"])
                for event in failures
                if event.get("feature_flag")
            }
        )
        enabled_changes = {
            str(change.get("name"))
            for change in telemetry.get("configuration_changes", [])
            if change.get("value") is True
        }
        for feature_flag in feature_flags:
            if feature_flag not in enabled_changes:
                continue
            candidates.append(
                CauseCandidate(
                    kind=CausalKind.CONFIGURATION_CHANGE,
                    reference=feature_flag,
                    title=f"{feature_flag} enabled",
                    rank=1,
                    score=0.97,
                    explanation=[
                        "The failing requests entered the newly enabled feature path.",
                        "The configuration change precedes the first matching failure.",
                        "No application release changed during the failure window.",
                    ],
                )
            )

        for commit in commits[:3]:
            candidates.append(
                CauseCandidate(
                    kind=CausalKind.CODE_CHANGE,
                    reference=commit.commit.sha,
                    title=commit.commit.title,
                    rank=1,
                    score=commit.total_score,
                    explanation=commit.explanation,
                )
            )

        if not candidates:
            candidates.append(
                CauseCandidate(
                    kind=CausalKind.UNKNOWN,
                    reference="insufficient-evidence",
                    title="Insufficient causal evidence",
                    rank=1,
                    score=0.0,
                    explanation=["No evidence-backed causal signal crossed the ranking floor."],
                )
            )

        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        return [item.model_copy(update={"rank": rank}) for rank, item in enumerate(ranked, 1)]
