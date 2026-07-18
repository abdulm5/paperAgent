from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.schema import classify_schema_revisions
from app.db.session import get_db

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str


class ReadinessResponse(HealthResponse):
    checks: dict[str, str]


@router.get("", response_model=HealthResponse, summary="Check API availability")
def health_check() -> HealthResponse:
    """Keep the original lightweight availability endpoint for local tooling."""
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        environment=settings.environment,
    )


@router.get("/live", response_model=HealthResponse, summary="Check process liveness")
def liveness_check(response: Response) -> HealthResponse:
    """Report only whether the API process can serve requests."""

    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status="alive",
        service=settings.service_name,
        environment=settings.environment,
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Check release readiness")
def readiness_check(
    response: Response,
    session: Session = Depends(get_db),
) -> ReadinessResponse:
    """Require a reachable database at an application-compatible schema revision.

    Redis is intentionally not an API-readiness dependency: PostgreSQL owns
    workflow intent and the transactional outbox is designed to absorb a Redis
    outage until the relay recovers. Schema compatibility follows the project's
    expand-contract migration policy so old and new pods can overlap safely.
    """

    response.headers["Cache-Control"] = "no-store"
    checks = {"database": "unavailable", "schema": "unknown"}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
        revisions = set(
            session.scalars(text("SELECT version_num FROM alembic_version")).all()
        )
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="not_ready",
            service=settings.service_name,
            environment=settings.environment,
            checks=checks,
        )

    minimum_application_generation: int | None = None
    try:
        minimum_application_generation = session.scalar(
            text(
                "SELECT minimum_application_generation "
                "FROM pageragent_schema_contract WHERE singleton_id = 1"
            )
        )
    except SQLAlchemyError:
        # An older schema legitimately lacks the marker. Roll back the failed
        # lookup so pooled PostgreSQL connections are returned in a clean state.
        session.rollback()

    schema_state = classify_schema_revisions(
        revisions,
        minimum_application_generation,
    )
    checks["schema"] = schema_state
    if schema_state not in {"current", "forward_compatible"}:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        readiness = "not_ready"
    else:
        readiness = "ready"
    return ReadinessResponse(
        status=readiness,
        service=settings.service_name,
        environment=settings.environment,
        checks=checks,
    )
