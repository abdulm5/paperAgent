from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock

from app.models import (
    CheckoutRequest,
    ConfigurationChange,
    DeploymentEvent,
    FeatureFlagResponse,
    PaymentMethod,
    ReleaseMetadata,
    ReleaseName,
    ResetResponse,
    ScenarioName,
    ScenarioStateResponse,
    TelemetryEvent,
    TelemetrySnapshot,
)

RELEASE_COMMITS = {
    ReleaseName.STABLE: "2ab1e90",
    ReleaseName.FAULTY: "8fa23c1",
    ReleaseName.OBSERVABILITY: "9c4e2d1",
}


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different mutation."""


class CheckoutState:
    """In-memory state makes each demo run deterministic and easy to reset."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_release = ReleaseName.STABLE
        self._deployed_at = datetime.now(UTC)
        self._events: list[TelemetryEvent] = []
        self._deployments: list[DeploymentEvent] = []
        self._scenario_id = "healthy"
        self._feature_flags: dict[str, bool] = {"wallet_validation_v2": False}
        self._dependencies: dict[str, str] = {"payment-gateway": "healthy"}
        self._configuration_changes: list[ConfigurationChange] = []
        self._idempotency_results: dict[
            str, tuple[str, DeploymentEvent | FeatureFlagResponse]
        ] = {}
        self.reset()

    def reset(self) -> ResetResponse:
        with self._lock:
            now = datetime.now(UTC)
            self._active_release = ReleaseName.STABLE
            self._deployed_at = now
            self._events = []
            self._scenario_id = "healthy"
            self._feature_flags = {"wallet_validation_v2": False}
            self._dependencies = {"payment-gateway": "healthy"}
            self._configuration_changes = []
            self._idempotency_results = {}
            self._deployments = [
                DeploymentEvent(
                    previous_release=None,
                    release=ReleaseName.STABLE,
                    commit_sha=RELEASE_COMMITS[ReleaseName.STABLE],
                    deployed_at=now,
                )
            ]
            return ResetResponse(active_release=self._active_release)

    def deploy(
        self, release: ReleaseName, idempotency_key: str | None = None
    ) -> DeploymentEvent:
        with self._lock:
            mutation = f"activate-release:{release.value}"
            if idempotency_key:
                cached = self._idempotency_results.get(idempotency_key)
                if cached is not None:
                    cached_mutation, cached_response = cached
                    if cached_mutation != mutation:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used for a different mutation"
                        )
                    if not isinstance(cached_response, DeploymentEvent):
                        raise RuntimeError("Cached idempotency response has the wrong type")
                    return cached_response

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
            if idempotency_key:
                self._idempotency_results[idempotency_key] = (mutation, event)
            return event

    def activate_scenario(self, scenario: ScenarioName) -> ScenarioStateResponse:
        with self._lock:
            self._scenario_id = scenario.value
            if scenario is ScenarioName.VALIDATION_BUG:
                self.deploy(ReleaseName.FAULTY)
            elif scenario is ScenarioName.PROVIDER_TIMEOUT:
                self.deploy(ReleaseName.OBSERVABILITY)
                self._dependencies["payment-gateway"] = "degraded"
            else:
                now = datetime.now(UTC)
                self._configuration_changes.append(
                    ConfigurationChange(
                        name="wallet_validation_v2",
                        previous_value=self._feature_flags["wallet_validation_v2"],
                        value=True,
                        changed_at=now,
                        actor="scenario-controller",
                    )
                )
                self._feature_flags["wallet_validation_v2"] = True
            return ScenarioStateResponse(
                scenario_id=scenario,
                active_release=self._active_release,
                feature_flags=dict(self._feature_flags),
                dependencies=dict(self._dependencies),
            )

    def disable_feature_flag(
        self, name: str, idempotency_key: str | None = None
    ) -> FeatureFlagResponse:
        with self._lock:
            if name not in self._feature_flags:
                raise KeyError(name)
            mutation = f"disable-feature-flag:{name}"
            if idempotency_key:
                cached = self._idempotency_results.get(idempotency_key)
                if cached is not None:
                    cached_mutation, cached_response = cached
                    if cached_mutation != mutation:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used for a different mutation"
                        )
                    if not isinstance(cached_response, FeatureFlagResponse):
                        raise RuntimeError("Cached idempotency response has the wrong type")
                    return cached_response

            now = datetime.now(UTC)
            previous = self._feature_flags[name]
            self._feature_flags[name] = False
            self._configuration_changes.append(
                ConfigurationChange(
                    name=name,
                    previous_value=previous,
                    value=False,
                    changed_at=now,
                    actor="pageragent-executor",
                )
            )
            response = FeatureFlagResponse(name=name, value=False, changed_at=now)
            if idempotency_key:
                self._idempotency_results[idempotency_key] = (mutation, response)
            return response

    def record_checkout(
        self,
        checkout: CheckoutRequest,
        request_id: str,
        trace_id: str,
    ) -> TelemetryEvent:
        with self._lock:
            sequence = len(self._events) + 1
            validation_failed = (
                self._active_release is ReleaseName.FAULTY
                and checkout.payment_method is PaymentMethod.DIGITAL_WALLET
            )
            provider_failed = (
                self._scenario_id == ScenarioName.PROVIDER_TIMEOUT.value
                and self._dependencies["payment-gateway"] == "degraded"
                and checkout.payment_method is PaymentMethod.BANK_TRANSFER
            )
            flag_failed = (
                self._scenario_id == ScenarioName.FEATURE_FLAG_REGRESSION.value
                and self._feature_flags["wallet_validation_v2"]
                and checkout.payment_method is PaymentMethod.DIGITAL_WALLET
            )
            failed = validation_failed or provider_failed or flag_failed
            baseline_latency = {
                PaymentMethod.CARD: 38,
                PaymentMethod.BANK_TRANSFER: 52,
                PaymentMethod.DIGITAL_WALLET: 45,
            }[checkout.payment_method]
            latency_penalty = 320 if provider_failed else 70 if failed else 0
            latency_ms = float(baseline_latency + sequence % 7 + latency_penalty)
            error_type = (
                "ValidationRuleMissing"
                if validation_failed
                else "UpstreamProviderTimeout"
                if provider_failed
                else "FeatureFlagRuleMismatch"
                if flag_failed
                else None
            )
            event = TelemetryEvent(
                timestamp=datetime.now(UTC),
                request_id=request_id,
                trace_id=trace_id,
                user_id=checkout.user_id,
                payment_method=checkout.payment_method,
                release=self._active_release,
                commit_sha=RELEASE_COMMITS[self._active_release],
                http_status=504 if provider_failed else 500 if failed else 201,
                outcome="failure" if failed else "success",
                latency_ms=latency_ms,
                error_type=error_type,
                scenario_id=self._scenario_id,
                upstream_dependency=("payment-gateway" if provider_failed else None),
                feature_flag=("wallet_validation_v2" if flag_failed else None),
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
                feature_flags=dict(self._feature_flags),
                dependencies=dict(self._dependencies),
                configuration_changes=list(self._configuration_changes),
                scenario_id=self._scenario_id,
                recent_events=list(events[-100:]),
            )


checkout_state = CheckoutState()
