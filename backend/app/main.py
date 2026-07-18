from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from opentelemetry.trace import SpanKind
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.router import api_router
from app.core.access_logging import install_access_log_redaction
from app.core.config import settings
from app.core.telemetry import configure_telemetry, current_trace_id, tracer

configure_telemetry()
install_access_log_redaction()


def response_security_headers() -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    }
    if settings.environment not in {"local", "test"}:
        headers.update(
            {
                "Content-Security-Policy": (
                    "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ),
                "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            }
        )
    return headers


class LowercaseHostMiddleware:
    """Normalize the case-insensitive Host value before exact allow-listing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            normalized_scope = dict(scope)
            normalized_scope["headers"] = [
                (name, value.lower() if name.lower() == b"host" else value)
                for name, value in scope["headers"]
            ]
            scope = normalized_scope
        await self.app(scope, receive, send)

app = FastAPI(
    title="PagerAgent API",
    version="0.1.0",
    description="Evidence-grounded incident response APIs.",
    docs_url="/docs" if settings.environment in {"local", "test"} else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment in {"local", "test"} else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.backend_cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        host.strip() for host in settings.backend_trusted_hosts.split(",") if host.strip()
    ],
)
app.add_middleware(LowercaseHostMiddleware)


@app.exception_handler(RequestValidationError)
async def sanitized_request_validation_error(
    _request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    """Reject invalid requests without reflecting attacker-controlled material."""
    return JSONResponse(status_code=422, content={"detail": "Request validation failed"})


@app.exception_handler(Exception)
async def sanitized_internal_server_error(
    _request: Request,
    _error: Exception,
) -> JSONResponse:
    """Keep the hosted header contract without reflecting unexpected failures."""

    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers=response_security_headers(),
    )


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
        for name, value in response_security_headers().items():
            response.headers.setdefault(name, value)
        return response

app.include_router(api_router, prefix="/api/v1")
