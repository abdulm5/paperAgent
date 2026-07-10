from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock

from app.models import (
    CheckoutRequest,
    DeploymentEvent,
    PaymentMethod,
    ReleaseMetadata,
    ReleaseName,
    ResetResponse,
    TelemetryEvent,
    TelemetrySnapshot,
)

RELEASE_COMMITS = {
    ReleaseName.STABLE: "2ab1e90",
    ReleaseName.FAULTY: "8fa23c1",
}


class CheckoutState:
    """In-memory state makes each demo run deterministic and easy to reset."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_release = ReleaseName.STABLE
        self._deployed_at = datetime.now(UTC)
        self._events: list[TelemetryEvent] = []
        self._deployments: list[DeploymentEvent] = []
        self.reset()

    def reset(self) -> ResetResponse:
        with self._lock:
            now = datetime.now(UTC)
            self._active_release = ReleaseName.STABLE
            self._deployed_at = now
            self._events = []
            self._deployments = [
                DeploymentEvent(
                    previous_release=None,
                    release=ReleaseName.STABLE,
                    commit_sha=RELEASE_COMMITS[ReleaseName.STABLE],
                    deployed_at=now,
                )
            ]
            return ResetResponse(active_release=self._active_release)

    def deploy(self, release: ReleaseName) -> DeploymentEvent:
        with self._lock:
            now = datetime.now(UTC)
            event = DeploymentEvent(
                previous_release=self._active_release,
                release=release,
                commit_sha=RELEASE_COMMITS[release],
                deployed_at=now,
            )
            self._active_release = release
            self._deployed_at = now
            self._deployments.append(event)
            return event

    def record_checkout(
        self,
        checkout: CheckoutRequest,
        request_id: str,
        trace_id: str,
    ) -> TelemetryEvent:
        with self._lock:
            sequence = len(self._events) + 1
            failed = (
                self._active_release is ReleaseName.FAULTY
                and checkout.payment_method is PaymentMethod.DIGITAL_WALLET
            )
            baseline_latency = {
                PaymentMethod.CARD: 38,
                PaymentMethod.BANK_TRANSFER: 52,
                PaymentMethod.DIGITAL_WALLET: 45,
            }[checkout.payment_method]
            latency_ms = float(baseline_latency + sequence % 7 + (70 if failed else 0))
            event = TelemetryEvent(
                timestamp=datetime.now(UTC),
                request_id=request_id,
                trace_id=trace_id,
                user_id=checkout.user_id,
                payment_method=checkout.payment_method,
                release=self._active_release,
                commit_sha=RELEASE_COMMITS[self._active_release],
                http_status=500 if failed else 201,
                outcome="failure" if failed else "success",
                latency_ms=latency_ms,
                error_type="ValidationRuleMissing" if failed else None,
            )
            self._events.append(event)
            return event

    def current_release(self) -> ReleaseMetadata:
        with self._lock:
            return ReleaseMetadata(
                name=self._active_release,
                commit_sha=RELEASE_COMMITS[self._active_release],
                deployed_at=self._deployed_at,
            )

    def snapshot(self, window_seconds: int = 300) -> TelemetrySnapshot:
        with self._lock:
            observed_at = datetime.now(UTC)
            window_started_at = observed_at - timedelta(seconds=window_seconds)
            events = [event for event in self._events if event.timestamp >= window_started_at]
            failures = [event for event in events if event.outcome == "failure"]
            latencies = sorted(event.latency_ms for event in events)
            percentile_index = max(0, ceil(0.95 * len(latencies)) - 1)
            p95_latency_ms = latencies[percentile_index] if latencies else 0.0
            request_count = len(events)

            return TelemetrySnapshot(
                observed_at=observed_at,
                window_started_at=window_started_at,
                window_seconds=window_seconds,
                current_release=self.current_release(),
                request_count=request_count,
                successful_request_count=request_count - len(failures),
                failed_request_count=len(failures),
                error_rate=len(failures) / request_count if request_count else 0.0,
                p95_latency_ms=p95_latency_ms,
                first_failure_at=failures[0].timestamp if failures else None,
                deployments=list(self._deployments),
                recent_events=list(events[-100:]),
            )


checkout_state = CheckoutState()
