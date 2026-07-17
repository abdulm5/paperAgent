from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind

from app.api.router import api_router
from app.core.access_logging import install_access_log_redaction
from app.core.config import settings
from app.core.telemetry import configure_telemetry, current_trace_id, tracer

configure_telemetry()
install_access_log_redaction()

app = FastAPI(
    title="PagerAgent API",
    version="0.1.0",
    description="Evidence-grounded incident response APIs.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.backend_cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_error(
    _request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    """Reject invalid requests without reflecting attacker-controlled material."""
    return JSONResponse(status_code=422, content={"detail": "Request validation failed"})


@app.middleware("http")
async def trace_http_request(request: Request, call_next):
    with tracer().start_as_current_span(
        f"{request.method} {request.url.path}",
        kind=SpanKind.SERVER,
        attributes={
            "http.request.method": request.method,
            "url.path": request.url.path,
        },
    ) as span:
        response = await call_next(request)
        span.set_attribute("http.response.status_code", response.status_code)
        trace_id = current_trace_id()
        if trace_id is not None:
            response.headers["X-Trace-ID"] = trace_id
        return response

app.include_router(api_router, prefix="/api/v1")
