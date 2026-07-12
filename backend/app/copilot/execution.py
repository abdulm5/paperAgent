from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.domain.proposals import ActionEnvelope


class ExecutionResult(BaseModel):
    response_payload: dict[str, Any]
    before_telemetry: dict[str, Any]
    after_telemetry: dict[str, Any]
    recovery_verified: bool


class MitigationExecutor(Protocol):
    version: str

    def execute(self, action: ActionEnvelope, idempotency_key: str) -> ExecutionResult: ...


class SimulatorMitigationExecutor:
    version = "checkout-simulator-executor-v1"

    def __init__(
        self,
        base_url: str,
        canary_requests: int,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.canary_requests = canary_requests
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds
        )

    def execute(self, action: ActionEnvelope, idempotency_key: str) -> ExecutionResult:
        if action.action_type != "rollback_service" or action.target_service != "checkout-api":
            raise ValueError("Executor policy rejected an unsupported mitigation action")
        if action.target_release != "stable-v1":
            raise ValueError("Executor policy only permits rollback to stable-v1")

        before_response = self.client.get("/telemetry")
        before_response.raise_for_status()
        before = before_response.json()

        deployment_response = self.client.post(
            f"/admin/releases/{action.target_release}/activate",
            headers={"X-Idempotency-Key": idempotency_key},
        )
        deployment_response.raise_for_status()
        deployment = deployment_response.json()

        statuses: list[int] = []
        for index in range(1, self.canary_requests + 1):
            payment_method = "digital_wallet" if index % 3 == 0 else "card"
            canary = self.client.post(
                "/checkout",
                json={
                    "user_id": f"recovery-canary-{index:03d}",
                    "cart_total_cents": 2_500 + index,
                    "payment_method": payment_method,
                },
                headers={
                    "X-Request-ID": f"recovery-{idempotency_key[-8:]}-{index:03d}",
                    "X-Trace-ID": f"recovery-trace-{idempotency_key[-8:]}-{index:03d}",
                },
            )
            statuses.append(canary.status_code)

        after_response = self.client.get("/telemetry")
        after_response.raise_for_status()
        after = after_response.json()
        deployed_at = datetime.fromisoformat(str(deployment["deployed_at"]).replace("Z", "+00:00"))
        recovery_events = [
            event
            for event in after.get("recent_events", [])
            if datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
            >= deployed_at
        ]
        recovery_failures = [
            event for event in recovery_events if event.get("outcome") == "failure"
        ]
        verified = (
            after.get("current_release", {}).get("name") == action.target_release
            and len(recovery_events) >= self.canary_requests
            and not recovery_failures
            and all(status < 400 for status in statuses)
        )
        return ExecutionResult(
            response_payload={
                "deployment": deployment,
                "canary_request_count": len(statuses),
                "canary_statuses": statuses,
                "recovery_failure_count": len(recovery_failures),
            },
            before_telemetry=before,
            after_telemetry=after,
            recovery_verified=verified,
        )
