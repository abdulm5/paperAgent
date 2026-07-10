import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import checkout_state


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    checkout_state.reset()
    yield
    checkout_state.reset()


def checkout_payload(payment_method: str = "card") -> dict[str, str | int]:
    return {
        "user_id": "user-123",
        "cart_total_cents": 4200,
        "payment_method": payment_method,
    }


def test_stable_release_accepts_all_supported_payment_methods() -> None:
    client = TestClient(app)

    response = client.post("/checkout", json=checkout_payload("digital_wallet"))

    assert response.status_code == 201
    assert response.json()["release"] == "stable-v1"
    assert client.get("/telemetry").json()["error_rate"] == 0


def test_faulty_release_only_fails_digital_wallet_requests() -> None:
    client = TestClient(app)
    deployment = client.post("/admin/releases/faulty-v2/activate")

    card = client.post("/checkout", json=checkout_payload("card"))
    wallet = client.post(
        "/checkout",
        json=checkout_payload("digital_wallet"),
        headers={"X-Request-ID": "known-request", "X-Trace-ID": "known-trace"},
    )
    telemetry = client.get("/telemetry").json()

    assert deployment.status_code == 200
    assert deployment.json()["commit_sha"] == "8fa23c1"
    assert card.status_code == 201
    assert wallet.status_code == 500
    assert wallet.json()["error_code"] == "ValidationRuleMissing"
    assert telemetry["request_count"] == 2
    assert telemetry["failed_request_count"] == 1
    assert telemetry["error_rate"] == 0.5
    assert telemetry["recent_events"][-1]["request_id"] == "known-request"
    assert telemetry["recent_events"][-1]["trace_id"] == "known-trace"


def test_prometheus_metrics_and_reset_expose_current_state() -> None:
    client = TestClient(app)
    client.post("/admin/releases/faulty-v2/activate")
    client.post("/checkout", json=checkout_payload("digital_wallet"))

    metrics = client.get("/metrics")
    reset = client.post("/admin/reset")
    telemetry = client.get("/telemetry").json()

    assert 'checkout_requests_total{status="failure"} 1' in metrics.text
    assert "checkout_error_rate 1.000000" in metrics.text
    assert reset.json()["active_release"] == "stable-v1"
    assert telemetry["request_count"] == 0


def test_checkout_emits_machine_readable_request_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(app)
    emitted_logs: list[str] = []
    monkeypatch.setattr("app.main.logger.info", emitted_logs.append)

    client.post(
        "/checkout",
        json=checkout_payload("card"),
        headers={"X-Request-ID": "evidence-request", "X-Trace-ID": "evidence-trace"},
    )
    log_event = json.loads(emitted_logs[-1])

    assert log_event["event"] == "checkout.request"
    assert log_event["request_id"] == "evidence-request"
    assert log_event["trace_id"] == "evidence-trace"
    assert log_event["commit_sha"] == "2ab1e90"
    assert log_event["http_status"] == 201
