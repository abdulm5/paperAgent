import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.postmortems import get_postmortem_service
from app.copilot.postmortems import (
    DeterministicPostmortemGenerator,
    OpenAIPostmortemGenerator,
    PostmortemGroundingValidator,
)
from app.domain.incidents import IncidentStatus, IncidentTransitionRequest
from app.domain.postmortems import (
    EditablePreventionItem,
    PostmortemFinalizeRequest,
    PostmortemUpdateRequest,
)
from app.domain.proposals import ProposalDecisionRequest
from app.evaluation.postmortems import evaluate_postmortem
from app.main import app
from app.services.incidents import IncidentService
from app.services.postmortems import (
    PostmortemConflictError,
    PostmortemGenerationError,
    PostmortemService,
    PostmortemVersionConflictError,
)
from tests.test_evaluations import run_scenario_investigation
from tests.test_proposals import (
    RecordingExecutor,
    incident_with_investigation,
    proposal_service,
)


def resolved_incident(db_session: Session) -> tuple[UUID, object]:
    incident_id, investigation = incident_with_investigation(db_session)
    proposal = proposal_service(db_session, RecordingExecutor()).generate(incident_id)
    incident_service = IncidentService(db_session)
    incident_service.transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.INVESTIGATING,
            actor="oncall@example.com",
            note="Reviewed ranked evidence.",
            expected_version=1,
        ),
    )
    approved = proposal_service(db_session, RecordingExecutor()).decide(
        proposal.id,
        ProposalDecisionRequest(
            decision="approve",
            actor="oncall@example.com",
            note="Approved rollback.",
        ),
    )
    assert approved.status == "verification_passed"
    incident_service.transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.RESOLVED,
            actor="incident-commander@example.com",
            note="Recovery remained healthy; closing incident.",
            expected_version=3,
        ),
    )
    return incident_id, investigation


def postmortem_service(db_session: Session, generator: object | None = None) -> PostmortemService:
    return PostmortemService(
        session=db_session,
        generator=generator or DeterministicPostmortemGenerator(),
        validator=PostmortemGroundingValidator(),
    )


def update_request(postmortem: object, **overrides: object) -> PostmortemUpdateRequest:
    content = postmortem.content
    values: dict[str, Any] = {
        "expected_version": postmortem.version,
        "actor": "incident-commander@example.com",
        "change_note": "Clarified the customer impact wording.",
        "title": content.title,
        "summary": content.summary.text,
        "root_cause": content.root_cause.text,
        "customer_impact": content.customer_impact.text,
        "detection": content.detection.text,
        "resolution": content.resolution.text,
        "what_went_well": [item.text for item in content.what_went_well],
        "what_went_poorly": [item.text for item in content.what_went_poorly],
        "prevention_items": [
            EditablePreventionItem(
                title=item.title,
                description=item.description,
                owner=item.owner,
                priority=item.priority,
                status=item.status,
            )
            for item in content.prevention_items
        ],
    }
    values.update(overrides)
    return PostmortemUpdateRequest.model_validate(values)


def test_postmortem_is_grounded_in_resolved_audit_record(db_session: Session) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    incident_record = service._load_incident(incident_id)
    proposal_record = incident_record.proposals[-1]
    investigation_record = next(
        item
        for item in incident_record.investigations
        if item.id == proposal_record.investigation_id
    )
    allowed_ids = service._allowed_evidence_ids(
        incident_record, investigation_record, proposal_record
    )

    postmortem = service.generate(incident_id)
    incident = IncidentService(db_session).get_detail(incident_id)

    assert postmortem.status == "draft"
    assert postmortem.version == 1
    assert "8fa23c1" in postmortem.content.root_cause.text
    assert "8 of 60" in postmortem.content.customer_impact.text
    assert len(postmortem.content.prevention_items) == 3
    assert len(postmortem.content.timeline) == len(incident.events) - 1
    assert postmortem.revisions[0].source == "generated"
    assert all(
        set(item.evidence_ids) <= allowed_ids
        for item in [
            postmortem.content.summary,
            postmortem.content.root_cause,
            postmortem.content.customer_impact,
            postmortem.content.detection,
            postmortem.content.resolution,
            *postmortem.content.what_went_well,
            *postmortem.content.what_went_poorly,
            *postmortem.content.prevention_items,
            *postmortem.content.timeline,
        ]
    )


def test_feature_flag_postmortem_preserves_the_actual_cause_and_action(
    db_session: Session,
) -> None:
    incident_id, _ = run_scenario_investigation(
        db_session, "checkout-feature-flag-regression"
    )
    proposal = proposal_service(db_session, RecordingExecutor()).generate(incident_id)
    incident_service = IncidentService(db_session)
    incident_service.transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.INVESTIGATING,
            actor="oncall@example.com",
            expected_version=1,
        ),
    )
    approved = proposal_service(db_session, RecordingExecutor()).decide(
        proposal.id,
        ProposalDecisionRequest(
            decision="approve",
            actor="oncall@example.com",
            note="Approved the typed feature flag change.",
        ),
    )
    assert approved.status == "verification_passed"
    incident_service.transition(
        incident_id,
        IncidentTransitionRequest(
            to_status=IncidentStatus.RESOLVED,
            actor="incident-commander@example.com",
            expected_version=3,
        ),
    )

    postmortem = postmortem_service(db_session).generate(incident_id)

    assert "wallet_validation_v2" in postmortem.content.root_cause.text
    assert "without an application deployment" in postmortem.content.root_cause.text
    assert "disabled wallet_validation_v2" in postmortem.content.resolution.text
    assert "8fa23c1" not in postmortem.content.summary.text


def test_postmortem_requires_resolved_incident(db_session: Session) -> None:
    incident_id, _ = incident_with_investigation(db_session)

    with pytest.raises(PostmortemGenerationError, match="must be resolved"):
        postmortem_service(db_session).generate(incident_id)


def test_edits_create_revisions_and_finalization_locks_document(
    db_session: Session,
) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    generated = service.generate(incident_id)
    original_citations = generated.content.customer_impact.evidence_ids
    original_timeline = generated.content.timeline

    edited = service.update(
        generated.id,
        update_request(
            generated,
            customer_impact=(
                "Eight checkout attempts failed in the observed window; failures were limited "
                "to digital-wallet requests."
            ),
        ),
    )

    assert edited.version == 2
    assert len(edited.revisions) == 2
    assert edited.revisions[-1].source == "operator_edit"
    assert edited.content.customer_impact.evidence_ids == original_citations
    assert edited.content.timeline == original_timeline
    with pytest.raises(PostmortemVersionConflictError):
        service.update(edited.id, update_request(generated))

    finalized = service.finalize(
        edited.id,
        PostmortemFinalizeRequest(
            expected_version=2,
            actor="incident-commander@example.com",
            note="Reviewed with the payments team.",
        ),
    )

    assert finalized.status == "final"
    assert finalized.version == 3
    assert finalized.finalized_by == "incident-commander@example.com"
    assert finalized.revisions[-1].source == "finalized"
    with pytest.raises(PostmortemConflictError, match="cannot be edited"):
        service.update(finalized.id, update_request(finalized))


def test_markdown_export_contains_audit_sections_and_evidence(db_session: Session) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    postmortem = service.generate(incident_id)

    markdown = service.export_markdown(postmortem.id)

    assert markdown.startswith("# checkout-api digital-wallet validation incident")
    assert "## Root cause" in markdown
    assert "## Timeline" in markdown
    assert "## Prevention items" in markdown
    assert "## Evidence index" in markdown
    assert "E-" in markdown


class InvalidPostmortemGenerator(DeterministicPostmortemGenerator):
    version = "invalid-postmortem-generator"

    def generate(self, context: dict[str, Any]):  # type: ignore[no-untyped-def]
        draft = super().generate(context)
        draft.root_cause.evidence_ids = ["invented-evidence"]
        return draft


def test_unknown_postmortem_citations_are_rejected(db_session: Session) -> None:
    incident_id, _ = resolved_incident(db_session)

    with pytest.raises(PostmortemGenerationError, match="unknown evidence"):
        postmortem_service(db_session, InvalidPostmortemGenerator()).generate(incident_id)


def test_openai_postmortem_uses_shared_structured_output_adapter(
    db_session: Session,
) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    incident = service._load_incident(incident_id)
    proposal = incident.proposals[-1]
    investigation = next(
        item for item in incident.investigations if item.id == proposal.investigation_id
    )
    context = service._generation_context(incident, investigation, proposal)
    draft = DeterministicPostmortemGenerator().generate(context)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": draft.model_dump_json()}
                        ],
                    }
                ]
            },
        )

    generator = OpenAIPostmortemGenerator(
        api_key="test-key",
        model_name="gpt-5.6-luna",
        base_url="https://api.openai.test/v1",
        timeout_seconds=5,
        client=httpx.Client(
            base_url="https://api.openai.test/v1",
            transport=httpx.MockTransport(handler),
        ),
    )

    result = generator.generate(context)

    assert result.title == draft.title
    assert requests[0]["text"]["format"]["name"] == "pageragent_postmortem"
    assert requests[0]["text"]["format"]["strict"] is True
    assert requests[0]["store"] is False


def test_final_postmortem_meets_quality_gate(db_session: Session) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    incident_record = service._load_incident(incident_id)
    proposal_record = incident_record.proposals[-1]
    investigation_record = next(
        item
        for item in incident_record.investigations
        if item.id == proposal_record.investigation_id
    )
    expected_event_ids = {str(event.id) for event in incident_record.events}
    allowed_ids = service._allowed_evidence_ids(
        incident_record, investigation_record, proposal_record
    )
    generated = service.generate(incident_id)
    final = service.finalize(
        generated.id,
        PostmortemFinalizeRequest(
            expected_version=1,
            actor="incident-commander@example.com",
        ),
    )
    assert evaluate_postmortem(final, allowed_ids, expected_event_ids).passed


def test_postmortem_api_supports_generate_edit_finalize_and_export(
    db_session: Session,
) -> None:
    incident_id, _ = resolved_incident(db_session)
    service = postmortem_service(db_session)
    app.dependency_overrides[get_postmortem_service] = lambda: service
    client = TestClient(app)

    created = client.post(f"/api/v1/incidents/{incident_id}/postmortem")
    postmortem = service.get(UUID(created.json()["id"]))
    updated = client.put(
        f"/api/v1/postmortems/{postmortem.id}",
        json=update_request(postmortem).model_dump(mode="json"),
    )
    finalized = client.post(
        f"/api/v1/postmortems/{postmortem.id}/finalize",
        json={
            "expected_version": 2,
            "actor": "incident-commander@example.com",
        },
    )
    exported = client.get(f"/api/v1/postmortems/{postmortem.id}/export")

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "final"
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "## Root cause" in exported.text
