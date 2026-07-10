from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.incidents import AlertIngestResponse, AlertPayload
from app.services.incidents import IncidentService

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("", response_model=AlertIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_alert(alert: AlertPayload, session: Session = Depends(get_db)) -> AlertIngestResponse:
    """Turn a validated monitoring alert into a detected incident."""
    incident, deduplicated = IncidentService(session).ingest_alert(alert)
    return AlertIngestResponse(incident=incident, deduplicated=deduplicated)
