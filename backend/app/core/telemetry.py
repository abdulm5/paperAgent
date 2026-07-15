from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    TraceFlags,
    set_span_in_context,
)

from app.core.config import settings

_configured = False


def configure_telemetry() -> None:
    """Install the OpenTelemetry SDK once per API, relay, or worker process."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.service_name,
                "deployment.environment": settings.environment,
            }
        )
    )
    if settings.otel_console_exporter:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True


def tracer():
    return trace.get_tracer("pageragent", "0.7.0")


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"{context.trace_id:032x}"


@contextmanager
def workflow_span(
    name: str,
    *,
    trace_id: str,
    kind: SpanKind = SpanKind.CONSUMER,
    attributes: dict[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """Continue a persisted workflow trace in another process."""
    parent = SpanContext(
        trace_id=int(trace_id, 16),
        span_id=1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=trace.TraceState(),
    )
    context = set_span_in_context(NonRecordingSpan(parent))
    with tracer().start_as_current_span(
        name,
        context=context,
        kind=kind,
        attributes=attributes,
    ) as span:
        yield span
