import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routes.workflows import workflow_event_stream
from app.auth.constants import DEFAULT_ORGANIZATION_ID, DEFAULT_ORGANIZATION_SLUG
from app.auth.dependencies import get_current_principal
from app.auth.permissions import permissions_for_role
from app.db.models import OrganizationRecord
from app.domain.auth import Principal, Role
from app.domain.incidents import AlertPayload
from app.domain.workflows import WorkflowType
from app.main import app
from app.services.incidents import IncidentNotFoundError, IncidentService
from app.workflows.store import WorkflowStore
from tests.conftest import TEST_USER_ID


def _alert(fingerprint: str = "checkout-api:error-rate") -> AlertPayload:
    now = datetime.now(UTC)
    return AlertPayload.model_validate(
        {
            "fingerprint": fingerprint,
            "source": "tenant-isolation-test",
            "service": "checkout-api",
            "severity": "critical",
            "summary": "Checkout failures exceeded the threshold.",
            "started_at": now.isoformat(),
            "detected_at": now.isoformat(),
            "metric": {
                "name": "http_server_error_rate",
                "value": 0.2,
                "threshold": 0.05,
                "window_seconds": 300,
                "request_count": 40,
                "failed_request_count": 8,
            },
            "release": {
                "name": "faulty-v2",
                "commit_sha": "8fa23c1",
                "deployed_at": now.isoformat(),
            },
            "telemetry_url": "http://checkout.test/telemetry",
        }
    )


def _second_organization(session: Session) -> OrganizationRecord:
    organization = OrganizationRecord(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        slug="other-operations",
        name="Other Operations",
    )
    session.add(organization)
    session.commit()
    return organization


def _principal(role: Role, organization_id: UUID = DEFAULT_ORGANIZATION_ID) -> Principal:
    return Principal(
        user_id=TEST_USER_ID,
        organization_id=organization_id,
        organization_slug=(
            DEFAULT_ORGANIZATION_SLUG
            if organization_id == DEFAULT_ORGANIZATION_ID
            else "other-operations"
        ),
        role=role,
        permissions=permissions_for_role(role),
    )


def test_same_fingerprint_deduplicates_inside_one_tenant_not_across_tenants(
    db_session: Session,
) -> None:
    other = _second_organization(db_session)
    default_service = IncidentService(db_session, DEFAULT_ORGANIZATION_ID)
    other_service = IncidentService(db_session, other.id)

    default_incident, default_duplicate = default_service.ingest_alert(_alert())
    other_incident, other_duplicate = other_service.ingest_alert(_alert())
    repeated_default, repeated_duplicate = default_service.ingest_alert(_alert())

    assert default_duplicate is False
    assert other_duplicate is False
    assert repeated_duplicate is True
    assert repeated_default.id == default_incident.id
    assert other_incident.id != default_incident.id
    assert [item.id for item in default_service.list_incidents()] == [default_incident.id]
    assert [item.id for item in other_service.list_incidents()] == [other_incident.id]


def test_cross_tenant_incident_and_workflow_ids_are_not_disclosed(
    db_session: Session,
) -> None:
    other = _second_organization(db_session)
    other_service = IncidentService(db_session, other.id)
    other_incident, _ = other_service.ingest_alert(_alert("other:fingerprint"))
    run = WorkflowStore(db_session, other.id).enqueue(
        other_incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        f"incident:{other_incident.id}:response",
    )
    db_session.commit()

    client = TestClient(app)
    incident_response = client.get(f"/api/v1/incidents/{other_incident.id}")
    workflow_response = client.get(f"/api/v1/workflows/{run.id}")

    assert incident_response.status_code == 404
    assert workflow_response.status_code == 404
    with pytest.raises(IncidentNotFoundError):
        IncidentService(db_session, DEFAULT_ORGANIZATION_ID).get_detail(other_incident.id)


def test_workflow_event_cursor_filters_before_loading_tenant_snapshots(
    db_session: Session,
) -> None:
    other = _second_organization(db_session)
    default_incident, _ = IncidentService(db_session).ingest_alert(_alert("default:event"))
    other_incident, _ = IncidentService(db_session, other.id).ingest_alert(_alert("other:event"))
    default_run = WorkflowStore(db_session).enqueue(
        default_incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        f"incident:{default_incident.id}:response",
    )
    other_run = WorkflowStore(db_session, other.id).enqueue(
        other_incident.id,
        WorkflowType.INCIDENT_RESPONSE,
        "investigate",
        f"incident:{other_incident.id}:response",
    )
    db_session.commit()

    default_events = WorkflowStore(db_session).events_after(0)
    other_events = WorkflowStore(db_session, other.id).events_after(0)

    assert default_events
    assert other_events
    assert {event.workflow_run_id for event in default_events} == {default_run.id}
    assert {event.workflow_run_id for event in other_events} == {other_run.id}


def test_workflow_event_stream_stops_when_the_signed_session_expires() -> None:
    class ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def read_after_expiry() -> str:
        stream = workflow_event_stream(
            ConnectedRequest(),  # type: ignore[arg-type]
            principal=_principal(Role.VIEWER),
            session_id=uuid4(),
            session_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            poll_seconds=0,
        )
        return await anext(stream)

    with pytest.raises(StopAsyncIteration):
        asyncio.run(read_after_expiry())


def test_dev_reset_deletes_only_the_authenticated_organization(
    db_session: Session,
) -> None:
    other = _second_organization(db_session)
    default_incident, _ = IncidentService(db_session).ingest_alert(_alert("default:reset"))
    other_incident, _ = IncidentService(db_session, other.id).ingest_alert(_alert("other:reset"))

    response = TestClient(app).delete("/api/v1/dev/incidents")

    assert response.status_code == 200
    assert response.json() == {"cleared_incidents": 1}
    with pytest.raises(IncidentNotFoundError):
        IncidentService(db_session).get_detail(default_incident.id)
    surviving = IncidentService(db_session, other.id).get_detail(other_incident.id)
    assert surviving.id == other_incident.id


def test_browser_actor_is_derived_from_the_verified_principal() -> None:
    client = TestClient(app)
    created = client.post("/api/v1/alerts", json=_alert().model_dump(mode="json"))
    assert created.status_code == 201
    incident = created.json()["incident"]

    transitioned = client.post(
        f"/api/v1/incidents/{incident['id']}/transitions",
        json={
            "to_status": "investigating",
            "expected_version": incident["version"],
            "actor": "attacker-controlled-name",
            "note": "Reviewed the evidence.",
        },
    )

    assert transitioned.status_code == 200, transitioned.text
    assert transitioned.json()["events"][-1]["actor"] == f"user:{TEST_USER_ID}"


@pytest.mark.parametrize(
    ("role", "expected_status"),
    [
        (Role.VIEWER, 403),
        (Role.RESPONDER, 404),
        (Role.INCIDENT_COMMANDER, 404),
        (Role.ADMIN, 404),
    ],
)
def test_investigation_permission_is_enforced_before_resource_lookup(
    role: Role,
    expected_status: int,
) -> None:
    app.dependency_overrides[get_current_principal] = lambda: _principal(role)

    response = TestClient(app).post(f"/api/v1/incidents/{uuid4()}/investigations")

    assert response.status_code == expected_status
