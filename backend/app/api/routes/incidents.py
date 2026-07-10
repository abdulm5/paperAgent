from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.domain.incidents import Incident, incident_store

router = APIRouter(tags=["incidents"])


class ResetResponse(BaseModel):
    cleared_incidents: int


@router.get("/incidents", response_model=list[Incident])
def list_incidents() -> list[Incident]:
    return incident_store.list()


@router.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: UUID) -> Incident:
    incident = incident_store.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident


@router.delete("/dev/incidents", response_model=ResetResponse)
def reset_incidents() -> ResetResponse:
    """Clear demo state. This endpoint is unavailable outside local/test environments."""
    if settings.environment not in {"local", "test"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ResetResponse(cleared_incidents=incident_store.clear())
