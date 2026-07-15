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


def test_provider_timeout_preserves_red_herring_deploy_and_dependency_evidence() -> None:
    client = TestClient(app)

    activation = client.post("/admin/scenarios/payment-provider-timeout/activate")
    card = client.post("/checkout", json=checkout_payload("card"))
    transfer = client.post("/checkout", json=checkout_payload("bank_transfer"))
    telemetry = client.get("/telemetry").json()

    assert activation.status_code == 200
    assert activation.json()["active_release"] == "observability-v3"
    assert card.status_code == 201
    assert transfer.status_code == 504
    assert transfer.json()["error_code"] == "UpstreamProviderTimeout"
    assert telemetry["dependencies"]["payment-gateway"] == "degraded"
    assert telemetry["recent_events"][-1]["upstream_dependency"] == "payment-gateway"
    assert telemetry["recent_events"][-1]["commit_sha"] == "9c4e2d1"


def test_feature_flag_scenario_recovers_after_typed_flag_change() -> None:
    client = TestClient(app)

    activation = client.post(
        "/admin/scenarios/checkout-feature-flag-regression/activate"
    )
    before = client.post("/checkout", json=checkout_payload("digital_wallet"))
    mitigation = client.post("/admin/feature-flags/wallet_validation_v2/disable")
    after = client.post("/checkout", json=checkout_payload("digital_wallet"))
    telemetry = client.get("/telemetry").json()

    assert activation.status_code == 200
    assert activation.json()["active_release"] == "stable-v1"
    assert before.status_code == 500
    assert before.json()["error_code"] == "FeatureFlagRuleMismatch"
    assert mitigation.status_code == 200
    assert mitigation.json()["value"] is False
    assert after.status_code == 201
    assert telemetry["feature_flags"]["wallet_validation_v2"] is False
    assert [change["value"] for change in telemetry["configuration_changes"]] == [
        True,
        False,
    ]


def test_release_activation_is_effectively_once_for_the_same_idempotency_key() -> None:
    client = TestClient(app)
    headers = {"X-Idempotency-Key": "proposal-release-123"}

    first = client.post("/admin/releases/faulty-v2/activate", headers=headers)
    replay = client.post("/admin/releases/faulty-v2/activate", headers=headers)
    telemetry = client.get("/telemetry").json()

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(telemetry["deployments"]) == 2
    assert [deployment["release"] for deployment in telemetry["deployments"]] == [
        "stable-v1",
        "faulty-v2",
    ]


def test_different_release_idempotency_keys_create_independent_mutations() -> None:
    client = TestClient(app)

    client.post(
        "/admin/releases/faulty-v2/activate",
        headers={"X-Idempotency-Key": "proposal-release-a"},
    )
    client.post(
        "/admin/releases/faulty-v2/activate",
        headers={"X-Idempotency-Key": "proposal-release-b"},
    )
    telemetry = client.get("/telemetry").json()

    assert len(telemetry["deployments"]) == 3
    assert [deployment["release"] for deployment in telemetry["deployments"]] == [
        "stable-v1",
        "faulty-v2",
        "faulty-v2",
    ]


def test_feature_flag_disable_is_effectively_once_per_idempotency_key() -> None:
    client = TestClient(app)
    client.post("/admin/scenarios/checkout-feature-flag-regression/activate")
    headers = {"X-Idempotency-Key": "proposal-flag-123"}

    first = client.post(
        "/admin/feature-flags/wallet_validation_v2/disable", headers=headers
    )
    replay = client.post(
        "/admin/feature-flags/wallet_validation_v2/disable", headers=headers
    )
    independent = client.post(
        "/admin/feature-flags/wallet_validation_v2/disable",
        headers={"X-Idempotency-Key": "proposal-flag-456"},
    )
    telemetry = client.get("/telemetry").json()

    assert replay.json() == first.json()
    assert independent.status_code == 200
    assert [change["value"] for change in telemetry["configuration_changes"]] == [
        True,
        False,
        False,
    ]


def test_idempotency_key_cannot_be_reused_for_a_different_mutation() -> None:
    client = TestClient(app)
    headers = {"X-Idempotency-Key": "proposal-conflict"}
    client.post("/admin/releases/faulty-v2/activate", headers=headers)

    conflict = client.post("/admin/releases/stable-v1/activate", headers=headers)
    telemetry = client.get("/telemetry").json()

    assert conflict.status_code == 409
    assert "different mutation" in conflict.json()["detail"]
    assert telemetry["current_release"]["name"] == "faulty-v2"
    assert len(telemetry["deployments"]) == 2
