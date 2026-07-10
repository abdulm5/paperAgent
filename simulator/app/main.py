import json
import logging
import sys
from uuid import uuid4

from fastapi import FastAPI, Header, Query, status
from fastapi.responses import JSONResponse, PlainTextResponse

from app.models import (
    CheckoutFailure,
    CheckoutRequest,
    CheckoutResponse,
    DeploymentEvent,
    ReleaseName,
    ResetResponse,
    TelemetrySnapshot,
)
from app.state import checkout_state

logger = logging.getLogger("checkout.telemetry")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

app = FastAPI(
    title="Simulated Checkout API",
    version="0.1.0",
    description="Deterministic production service used by PagerAgent scenarios.",
)


def emit_structured_log(event_name: str, payload: dict[str, object]) -> None:
    logger.info(json.dumps({"event": event_name, **payload}, separators=(",", ":")))


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "checkout-api"}


@app.post(
    "/checkout",
    status_code=status.HTTP_201_CREATED,
    responses={500: {"model": CheckoutFailure}},
)
def create_checkout(
    checkout: CheckoutRequest,
    x_request_id: str | None = Header(default=None),
    x_trace_id: str | None = Header(default=None),
) -> JSONResponse:
    request_id = x_request_id or f"req-{uuid4().hex[:12]}"
    trace_id = x_trace_id or f"trace-{uuid4().hex[:16]}"
    event = checkout_state.record_checkout(checkout, request_id, trace_id)
    emit_structured_log("checkout.request", event.model_dump(mode="json"))

    if event.outcome == "failure":
        failure = CheckoutFailure(
            error_code=event.error_type or "UnknownCheckoutFailure",
            message="No validation rule exists for digital_wallet in this release.",
            request_id=request_id,
            trace_id=trace_id,
            release=event.release,
        )
        return JSONResponse(status_code=500, content=failure.model_dump(mode="json"))

    response = CheckoutResponse(
        order_id=f"order-{request_id}",
        request_id=request_id,
        trace_id=trace_id,
        release=event.release,
    )
    return JSONResponse(status_code=201, content=response.model_dump(mode="json"))


@app.post("/admin/releases/{release}/activate", response_model=DeploymentEvent)
def activate_release(release: ReleaseName) -> DeploymentEvent:
    event = checkout_state.deploy(release)
    emit_structured_log("deployment.activated", event.model_dump(mode="json"))
    return event


@app.post("/admin/reset", response_model=ResetResponse)
def reset_simulator() -> ResetResponse:
    response = checkout_state.reset()
    emit_structured_log("simulator.reset", response.model_dump(mode="json"))
    return response


@app.get("/telemetry", response_model=TelemetrySnapshot)
def get_telemetry(window_seconds: int = Query(default=300, ge=1, le=3600)) -> TelemetrySnapshot:
    return checkout_state.snapshot(window_seconds)


@app.get("/metrics", response_class=PlainTextResponse)
def get_prometheus_metrics() -> str:
    snapshot = checkout_state.snapshot()
    release = snapshot.current_release.name.value
    return "\n".join(
        [
            "# HELP checkout_requests_total Checkout requests observed in the current window.",
            "# TYPE checkout_requests_total gauge",
            f'checkout_requests_total{{status="success"}} {snapshot.successful_request_count}',
            f'checkout_requests_total{{status="failure"}} {snapshot.failed_request_count}',
            "# HELP checkout_error_rate Fraction of checkout requests returning errors.",
            "# TYPE checkout_error_rate gauge",
            f"checkout_error_rate {snapshot.error_rate:.6f}",
            "# HELP checkout_latency_p95_ms Simulated p95 checkout latency in milliseconds.",
            "# TYPE checkout_latency_p95_ms gauge",
            f"checkout_latency_p95_ms {snapshot.p95_latency_ms:.2f}",
            "# HELP checkout_release_info Active checkout release metadata.",
            "# TYPE checkout_release_info gauge",
            (
                f'checkout_release_info{{release="{release}",'
                f'commit_sha="{snapshot.current_release.commit_sha}"}} 1'
            ),
            "",
        ]
    )
