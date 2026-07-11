from pydantic import BaseModel

from app.domain.investigations import InvestigationDetail


class InvestigationGroundTruth(BaseModel):
    faulty_commit: str
    expected_runbook: str
    affected_payment_method: str
    expected_impacted_requests: int


class InvestigationMetrics(BaseModel):
    commit_top_1: float
    commit_top_3: float
    runbook_top_1: float
    impact_count_accuracy: float
    affected_attribute_accuracy: float
    evidence_traceability: float

    @property
    def passed(self) -> bool:
        return (
            self.commit_top_3 == 1.0
            and self.runbook_top_1 == 1.0
            and self.impact_count_accuracy == 1.0
            and self.affected_attribute_accuracy == 1.0
            and self.evidence_traceability == 1.0
        )


def evaluate_investigation(
    investigation: InvestigationDetail,
    ground_truth: InvestigationGroundTruth,
) -> InvestigationMetrics:
    """Score ranked output against scenario truth without subjective model judging."""

    commit_shas = [candidate.commit_sha for candidate in investigation.commit_candidates]
    runbook_ids = [match.runbook_id for match in investigation.runbook_matches]
    impacted_requests = sum(cluster.failure_count for cluster in investigation.error_clusters)
    affected_payment_methods = {
        str(method)
        for cluster in investigation.error_clusters
        for method in cluster.affected_attributes.get("payment_methods", [])
    }
    derived_records = [
        *investigation.error_clusters,
        *investigation.commit_candidates,
        *investigation.runbook_matches,
    ]

    return InvestigationMetrics(
        commit_top_1=float(bool(commit_shas) and commit_shas[0] == ground_truth.faulty_commit),
        commit_top_3=float(ground_truth.faulty_commit in commit_shas[:3]),
        runbook_top_1=float(
            bool(runbook_ids) and runbook_ids[0] == ground_truth.expected_runbook
        ),
        impact_count_accuracy=float(
            impacted_requests == ground_truth.expected_impacted_requests
        ),
        affected_attribute_accuracy=float(
            ground_truth.affected_payment_method in affected_payment_methods
        ),
        evidence_traceability=float(
            bool(investigation.evidence)
            and bool(derived_records)
            and all(record.evidence_ids for record in derived_records)
        ),
    )
