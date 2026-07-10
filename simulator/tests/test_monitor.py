from typing import Any

from app.monitor import build_alert, should_alert


def telemetry_snapshot(error_rate: float = 0.2, request_count: int = 40) -> dict[str, Any]:
    return {
        "service": "checkout-api",
        "observed_at": "2026-07-09T18:00:05Z",
        "window_seconds": 300,
        "current_release": {
            "name": "faulty-v2",
            "commit_sha": "8fa23c1",
            "deployed_at": "2026-07-09T17:59:55Z",
        },
        "request_count": request_count,
        "failed_request_count": int(request_count * error_rate),
        "error_rate": error_rate,
        "first_failure_at": "2026-07-09T18:00:00Z" if error_rate else None,
    }


def test_threshold_requires_enough_requests_and_an_actual_failure() -> None:
    assert should_alert(telemetry_snapshot(), threshold=0.05, minimum_requests=20)
    assert not should_alert(telemetry_snapshot(request_count=10), 0.05, 20)
    assert not should_alert(telemetry_snapshot(error_rate=0), 0.05, 20)


def test_alert_preserves_metric_and_release_evidence() -> None:
    alert = build_alert(telemetry_snapshot(), 0.05, "http://checkout-api:8100")

    assert alert["severity"] == "critical"
    assert alert["metric"]["failed_request_count"] == 8
    assert alert["release"]["commit_sha"] == "8fa23c1"
    assert alert["telemetry_url"] == "http://checkout-api:8100/telemetry"
