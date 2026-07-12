from pydantic import BaseModel

from app.domain.postmortems import PostmortemDetail, PostmortemStatus


class PostmortemMetrics(BaseModel):
    required_sections: float
    citation_coverage: float
    timeline_coverage: float
    root_cause_accuracy: float
    impact_accuracy: float
    prevention_depth: float
    revision_audit: float

    @property
    def passed(self) -> bool:
        return all(value == 1.0 for value in self.model_dump().values())


def evaluate_postmortem(
    postmortem: PostmortemDetail,
    allowed_evidence_ids: set[str],
    expected_event_ids: set[str],
) -> PostmortemMetrics:
    content = postmortem.content
    sections = [
        content.summary,
        content.root_cause,
        content.customer_impact,
        content.detection,
        content.resolution,
    ]
    grounded_items = [
        *sections,
        *content.what_went_well,
        *content.what_went_poorly,
        *content.prevention_items,
        *content.timeline,
    ]
    cited_ids = {
        evidence_id for item in grounded_items for evidence_id in item.evidence_ids
    }
    timeline_ids = {
        evidence_id for item in content.timeline for evidence_id in item.evidence_ids
    }
    return PostmortemMetrics(
        required_sections=float(all(section.text.strip() for section in sections)),
        citation_coverage=float(
            all(item.evidence_ids for item in grounded_items)
            and cited_ids <= allowed_evidence_ids
        ),
        timeline_coverage=float(timeline_ids == expected_event_ids),
        root_cause_accuracy=float("8fa23c1" in content.root_cause.text),
        impact_accuracy=float(
            "8 of 60" in content.customer_impact.text
            and "digital_wallet" in content.customer_impact.text
        ),
        prevention_depth=float(len(content.prevention_items) >= 3),
        revision_audit=float(
            bool(postmortem.revisions)
            and postmortem.revisions[-1].version == postmortem.version
            and (
                postmortem.status is not PostmortemStatus.FINAL
                or postmortem.revisions[-1].source == "finalized"
            )
        ),
    )
