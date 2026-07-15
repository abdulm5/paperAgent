from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_principal, require_permission
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.investigations import InvestigationDetail
from app.services.incidents import IncidentNotFoundError
from app.services.investigations import (
    InvestigationExecutionError,
    InvestigationNotFoundError,
    InvestigationService,
    build_investigation_service,
)

router = APIRouter(prefix="/incidents/{incident_id}/investigations", tags=["investigations"])


def get_investigation_service(
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> InvestigationService:
    return build_investigation_service(session, principal.organization_id)


@router.post("", response_model=InvestigationDetail, status_code=status.HTTP_201_CREATED)
def run_investigation(
    incident_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.INVESTIGATIONS_RUN)
    ),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationDetail:
    try:
        return service.run(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except InvestigationExecutionError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.get("/latest", response_model=InvestigationDetail)
def get_latest_investigation(
    incident_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.INCIDENTS_READ)
    ),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationDetail:
    try:
        return service.get_latest(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except InvestigationNotFoundError as error:
        raise HTTPException(status_code=404, detail="Investigation not found") from error
