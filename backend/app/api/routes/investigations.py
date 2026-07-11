from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.investigations import InvestigationDetail
from app.services.incidents import IncidentNotFoundError
from app.services.investigations import (
    InvestigationExecutionError,
    InvestigationNotFoundError,
    InvestigationService,
    build_investigation_service,
)

router = APIRouter(prefix="/incidents/{incident_id}/investigations", tags=["investigations"])


def get_investigation_service(session: Session = Depends(get_db)) -> InvestigationService:
    return build_investigation_service(session)


@router.post("", response_model=InvestigationDetail, status_code=status.HTTP_201_CREATED)
def run_investigation(
    incident_id: UUID,
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
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationDetail:
    try:
        return service.get_latest(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except InvestigationNotFoundError as error:
        raise HTTPException(status_code=404, detail="Investigation not found") from error
