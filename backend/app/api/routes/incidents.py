from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_permission
from app.core.config import settings
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.incidents import (
    IncidentDetail,
    IncidentStatus,
    IncidentSummary,
    IncidentTransitionInput,
    IncidentTransitionRequest,
    ResetResponse,
)
from app.services.incidents import (
    IncidentNotFoundError,
    IncidentService,
    IncidentVersionConflictError,
    InvalidTransitionError,
)

router = APIRouter(tags=["incidents"])


@router.get("/incidents", response_model=list[IncidentSummary])
def list_incidents(
    principal: Principal = Depends(require_permission(Permission.INCIDENTS_READ)),
    session: Session = Depends(get_db),
) -> list[IncidentSummary]:
    return IncidentService(session, principal.organization_id).list_incidents()


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(
    incident_id: UUID,
    principal: Principal = Depends(require_permission(Permission.INCIDENTS_READ)),
    session: Session = Depends(get_db),
) -> IncidentDetail:
    try:
        return IncidentService(session, principal.organization_id).get_detail(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found"
        ) from error


@router.post("/incidents/{incident_id}/transitions", response_model=IncidentDetail)
def transition_incident(
    incident_id: UUID,
    request: IncidentTransitionInput,
    principal: Principal = Depends(
        require_permission(Permission.INCIDENTS_TRANSITION)
    ),
    session: Session = Depends(get_db),
) -> IncidentDetail:
    service = IncidentService(session, principal.organization_id)
    if request.to_status is IncidentStatus.MITIGATED:
        try:
            current = service.get_detail(incident_id)
        except IncidentNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found"
            ) from error
        if current.version != request.expected_version:
            error = IncidentVersionConflictError(current.version)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Incidents are marked mitigated only after an approved action passes "
                "recovery verification"
            ),
        )
    if (
        request.to_status is IncidentStatus.RESOLVED
        and Permission.INCIDENTS_RESOLVE not in principal.permissions
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolving an incident requires incident commander permission",
        )
    trusted_request = IncidentTransitionRequest(
        **request.model_dump(),
        actor=principal.actor,
    )
    try:
        return service.transition(
            incident_id,
            trusted_request,
            enqueue_postmortem=(
                trusted_request.to_status is IncidentStatus.RESOLVED
                and settings.auto_generate_postmortems
            ),
        )
    except IncidentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found"
        ) from error
    except (InvalidTransitionError, IncidentVersionConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.delete("/dev/incidents", response_model=ResetResponse)
def reset_incidents(
    principal: Principal = Depends(
        require_permission(Permission.ORGANIZATION_RESET)
    ),
    session: Session = Depends(get_db),
) -> ResetResponse:
    """Clear demo state. This endpoint is unavailable outside local/test environments."""
    if settings.environment not in {"local", "test"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return ResetResponse(
        cleared_incidents=IncidentService(session, principal.organization_id).clear()
    )
