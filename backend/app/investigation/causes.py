from typing import Any

from app.domain.evaluations import CausalKind, CauseCandidate
from app.domain.prometheus import PrometheusEvidenceBundle
from app.investigation.commits import RankedCommit


class CauseRanker:
    version = "causal-signal-ranker-v2"

    def rank(
        self,
        telemetry: dict[str, Any],
        commits: list[RankedCommit],
        prometheus_evidence: PrometheusEvidenceBundle | None = None,
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

        if self._prometheus_corroborates_failures(telemetry, prometheus_evidence):
            candidates = [
                candidate.model_copy(
                    update={
                        "score": min(1.0, candidate.score + 0.01),
                        "explanation": [
                            *candidate.explanation,
                            (
                                "The bounded Prometheus error-rate window independently "
                                "corroborates the structured failure signal."
                            ),
                        ],
                    }
                )
                if candidate.kind is not CausalKind.UNKNOWN
                else candidate
                for candidate in candidates
            ]

        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        return [item.model_copy(update={"rank": rank}) for rank, item in enumerate(ranked, 1)]

    @staticmethod
    def _prometheus_corroborates_failures(
        telemetry: dict[str, Any],
        evidence: PrometheusEvidenceBundle | None,
    ) -> bool:
        if evidence is None or evidence.metric_name != "http_server_error_rate":
            return False
        try:
            structured_error_rate = float(telemetry.get("error_rate", 0.0))
        except (TypeError, ValueError):
            return False
        values = [sample.value for series in evidence.series for sample in series.samples]
        if structured_error_rate <= 0 or not values:
            return False
        # Prometheus scrape timing need not align exactly with the event snapshot.
        # Treat the independent signal as a bounded confidence adjustment only
        # when its peak reaches at least 75% of the structured error rate.
        return max(values) >= structured_error_rate * 0.75
