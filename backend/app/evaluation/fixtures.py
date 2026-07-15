from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.evaluations import ScenarioContract

PAYMENT_SEQUENCE = ["card", "card", "bank_transfer", "card", "digital_wallet"]


def build_telemetry_fixture(scenario: ScenarioContract) -> dict[str, Any]:
    """Build repeatable telemetry directly from the versioned scenario contract."""

    simulation = scenario.simulation
    baseline_time = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    activation_time = baseline_time + timedelta(seconds=simulation.healthy_requests)
    total_requests = simulation.healthy_requests + simulation.outage_requests
    events: list[dict[str, Any]] = []

    for index in range(1, total_requests + 1):
        in_outage = index > simulation.healthy_requests
        outage_index = index - simulation.healthy_requests
        failed = in_outage and outage_index % simulation.failure_every == 0
        payment_method = (
            simulation.payment_method
            if failed
            else PAYMENT_SEQUENCE[(index - 1) % len(PAYMENT_SEQUENCE)]
        )
        release = simulation.active_release if in_outage else "stable-v1"
        commit = simulation.active_commit if in_outage else "2ab1e90"
        timestamp = baseline_time + timedelta(seconds=index)
        events.append(
            {
                "timestamp": timestamp.isoformat(),
                "service": scenario.service,
                "endpoint": "/checkout",
                "request_id": f"{scenario.id}-{index:03d}",
                "trace_id": f"trace-{scenario.id}-{index:03d}",
                "payment_method": payment_method,
                "release": release,
                "commit_sha": commit,
                "http_status": simulation.failure_status if failed else 201,
                "outcome": "failure" if failed else "success",
                "latency_ms": 380.0 if failed else 42.0,
                "error_type": simulation.error_type if failed else None,
                "scenario_id": scenario.id,
                "upstream_dependency": (
                    simulation.upstream_dependency if failed else None
                ),
                "feature_flag": simulation.feature_flag if failed else None,
            }
        )

    failures = [event for event in events if event["outcome"] == "failure"]
    deployments = [
        {
            "previous_release": None,
            "release": "stable-v1",
            "commit_sha": "2ab1e90",
            "deployed_at": baseline_time.isoformat(),
        }
    ]
    if simulation.active_release != "stable-v1":
        deployments.append(
            {
                "previous_release": "stable-v1",
                "release": simulation.active_release,
                "commit_sha": simulation.active_commit,
                "deployed_at": activation_time.isoformat(),
            }
        )
    configuration_changes = []
    feature_flags = {"wallet_validation_v2": False}
    if simulation.feature_flag:
        feature_flags[simulation.feature_flag] = True
        configuration_changes.append(
            {
                "name": simulation.feature_flag,
                "previous_value": False,
                "value": True,
                "changed_at": activation_time.isoformat(),
                "actor": "scenario-controller",
            }
        )
    dependencies = {"payment-gateway": "healthy"}
    if simulation.upstream_dependency:
        dependencies[simulation.upstream_dependency] = "degraded"

    return {
        "service": scenario.service,
        "observed_at": (baseline_time + timedelta(seconds=total_requests + 5)).isoformat(),
        "window_started_at": (baseline_time - timedelta(seconds=235)).isoformat(),
        "window_seconds": 300,
        "current_release": {
            "name": simulation.active_release,
            "commit_sha": simulation.active_commit,
            "deployed_at": activation_time.isoformat(),
        },
        "request_count": total_requests,
        "successful_request_count": total_requests - len(failures),
        "failed_request_count": len(failures),
        "error_rate": len(failures) / total_requests,
        "p95_latency_ms": 380.0,
        "first_failure_at": failures[0]["timestamp"] if failures else None,
        "deployments": deployments,
        "feature_flags": feature_flags,
        "dependencies": dependencies,
        "configuration_changes": configuration_changes,
        "scenario_id": scenario.id,
        "recent_events": events,
    }
