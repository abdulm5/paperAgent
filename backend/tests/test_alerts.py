from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.domain.incidents import incident_store
from app.main import app


@pytest.fixture(autouse=True)
def clear_incidents() -> Iterator[None]:
    incident_store.clear()
    yield
    incident_store.clear()


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


def test_alert_creates_an_incident_and_duplicate_is_deduplicated() -> None:
    client = TestClient(app)

    first = client.post("/api/v1/alerts", json=alert_payload())
    duplicate = client.post("/api/v1/alerts", json=alert_payload())
    incidents = client.get("/api/v1/incidents")

    assert first.status_code == 201
    assert first.json()["deduplicated"] is False
    assert duplicate.status_code == 201
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["incident"]["id"] == first.json()["incident"]["id"]
    assert len(incidents.json()) == 1
    assert incidents.json()[0]["alert"]["metric"]["failed_request_count"] == 8


def test_alert_rejects_an_impossible_timeline() -> None:
    payload = alert_payload()
    payload["started_at"] = "2026-07-09T18:01:00Z"

    response = TestClient(app).post("/api/v1/alerts", json=payload)

    assert response.status_code == 422


def test_local_reset_clears_demo_incidents() -> None:
    client = TestClient(app)
    client.post("/api/v1/alerts", json=alert_payload())

    response = client.delete("/api/v1/dev/incidents")

    assert response.status_code == 200
    assert response.json() == {"cleared_incidents": 1}
    assert client.get("/api/v1/incidents").json() == []
