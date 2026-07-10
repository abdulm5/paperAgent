from fastapi.testclient import TestClient

from app.main import app


def alert_payload() -> dict[str, object]:
    return {
        "fingerprint": "checkout-api:http-server-error-rate:faulty-v2",
        "source": "simulated-threshold-evaluator",
        "service": "checkout-api",
        "severity": "critical",
        "summary": "Checkout API error rate is 20.0%, above the 5.0% threshold.",
        "started_at": "2026-07-09T18:00:00Z",
        "detected_at": "2026-07-09T18:00:05Z",
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
            "deployed_at": "2026-07-09T17:59:55Z",
        },
        "telemetry_url": "http://checkout-api:8100/telemetry",
    }


def test_alert_is_persisted_and_duplicate_is_attached_to_same_incident() -> None:
    client = TestClient(app)

    first = client.post("/api/v1/alerts", json=alert_payload())
    duplicate = client.post("/api/v1/alerts", json=alert_payload())
    incidents = client.get("/api/v1/incidents")
    detail = client.get(f"/api/v1/incidents/{first.json()['incident']['id']}")

    assert first.status_code == 201
    assert first.json()["deduplicated"] is False
    assert duplicate.status_code == 201
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["incident"]["id"] == first.json()["incident"]["id"]
    assert len(incidents.json()) == 1
    assert detail.json()["alert_count"] == 2
    assert [event["event_type"] for event in detail.json()["events"]] == [
        "incident.detected",
        "alert.deduplicated",
    ]
    assert detail.json()["alert"]["metric"]["failed_request_count"] == 8


def test_incident_lifecycle_is_ordered_and_version_checked() -> None:
    client = TestClient(app)
    created = client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]
    incident_id = created["id"]

    investigating = client.post(
        f"/api/v1/incidents/{incident_id}/transitions",
        json={
            "to_status": "investigating",
            "actor": "demo-operator",
            "note": "Investigating checkout failures.",
            "expected_version": 1,
        },
    )
    stale = client.post(
        f"/api/v1/incidents/{incident_id}/transitions",
        json={
            "to_status": "mitigated",
            "actor": "second-operator",
            "expected_version": 1,
        },
    )
    mitigated = client.post(
        f"/api/v1/incidents/{incident_id}/transitions",
        json={
            "to_status": "mitigated",
            "actor": "demo-operator",
            "note": "Rollback completed.",
            "expected_version": 2,
        },
    )
    resolved = client.post(
        f"/api/v1/incidents/{incident_id}/transitions",
        json={
            "to_status": "resolved",
            "actor": "demo-operator",
            "note": "Error rate remained below threshold.",
            "expected_version": 3,
        },
    )

    assert investigating.status_code == 200
    assert investigating.json()["status"] == "investigating"
    assert investigating.json()["version"] == 2
    assert stale.status_code == 409
    assert "current version is 2" in stale.json()["detail"]
    assert mitigated.json()["status"] == "mitigated"
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None
    assert len(resolved.json()["events"]) == 4


def test_resolved_fingerprint_can_create_a_new_incident() -> None:
    client = TestClient(app)
    first = client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]
    incident_id = first["id"]
    transitions = ["investigating", "mitigated", "resolved"]
    for version, next_status in enumerate(transitions, start=1):
        response = client.post(
            f"/api/v1/incidents/{incident_id}/transitions",
            json={
                "to_status": next_status,
                "actor": "demo-operator",
                "expected_version": version,
            },
        )
        assert response.status_code == 200

    repeated_outage = client.post("/api/v1/alerts", json=alert_payload())

    assert repeated_outage.status_code == 201
    assert repeated_outage.json()["deduplicated"] is False
    assert repeated_outage.json()["incident"]["id"] != incident_id
    assert len(client.get("/api/v1/incidents").json()) == 2


def test_invalid_transition_is_rejected_without_mutating_timeline() -> None:
    client = TestClient(app)
    incident = client.post("/api/v1/alerts", json=alert_payload()).json()["incident"]

    response = client.post(
        f"/api/v1/incidents/{incident['id']}/transitions",
        json={
            "to_status": "resolved",
            "actor": "demo-operator",
            "expected_version": 1,
        },
    )
    detail = client.get(f"/api/v1/incidents/{incident['id']}").json()

    assert response.status_code == 409
    assert detail["status"] == "detected"
    assert len(detail["events"]) == 1


def test_alert_rejects_an_impossible_timeline() -> None:
    payload = alert_payload()
    payload["started_at"] = "2026-07-09T18:01:00Z"

    response = TestClient(app).post("/api/v1/alerts", json=payload)

    assert response.status_code == 422


def test_local_reset_clears_persisted_demo_incidents() -> None:
    client = TestClient(app)
    client.post("/api/v1/alerts", json=alert_payload())

    response = client.delete("/api/v1/dev/incidents")

    assert response.status_code == 200
    assert response.json() == {"cleared_incidents": 1}
    assert client.get("/api/v1/incidents").json() == []
