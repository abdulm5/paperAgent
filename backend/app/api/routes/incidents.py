from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.domain.incidents import (
    IncidentDetail,
    IncidentStatus,
    IncidentSummary,
    IncidentTransitionRequest,
    ResetResponse,
)
from app.services.incidents import (
    IncidentNotFoundError,
    IncidentService,
    IncidentVersionConflictError,
    InvalidTransitionError,
)
from app.tasks.postmortems import run_postmortem_task

router = APIRouter(tags=["incidents"])


@router.get("/incidents", response_model=list[IncidentSummary])
def list_incidents(session: Session = Depends(get_db)) -> list[IncidentSummary]:
    return IncidentService(session).list_incidents()


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(incident_id: UUID, session: Session = Depends(get_db)) -> IncidentDetail:
    try:
        return IncidentService(session).get_detail(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found"
        ) from error


@router.post("/incidents/{incident_id}/transitions", response_model=IncidentDetail)
def transition_incident(
    incident_id: UUID,
    request: IncidentTransitionRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> IncidentDetail:
    try:
        incident = IncidentService(session).transition(incident_id, request)
        if (
            request.to_status is IncidentStatus.RESOLVED
            and settings.auto_generate_postmortems
        ):
            background_tasks.add_task(run_postmortem_task, incident_id)
        return incident
    except IncidentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found"
        ) from error
    except (InvalidTransitionError, IncidentVersionConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.delete("/dev/incidents", response_model=ResetResponse)
def reset_incidents(session: Session = Depends(get_db)) -> ResetResponse:
    """Clear demo state. This endpoint is unavailable outside local/test environments."""
    if settings.environment not in {"local", "test"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ResetResponse(cleared_incidents=IncidentService(session).clear())
