from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str


@router.get("", response_model=HealthResponse, summary="Check API availability")
def health_check() -> HealthResponse:
    """Return process health; dependency checks will be added with persistence."""
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        environment=settings.environment,
    )
