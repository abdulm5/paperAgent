from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_ingest_context
from app.core.config import settings
from app.db.session import get_db
from app.domain.auth import IngestContext
from app.domain.incidents import AlertIngestResponse, AlertPayload
from app.services.incidents import IncidentService

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("", response_model=AlertIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_alert(
    alert: AlertPayload,
    ingest: IngestContext = Depends(require_ingest_context),
    session: Session = Depends(get_db),
) -> AlertIngestResponse:
    """Turn a validated monitoring alert into a detected incident."""
    incident, deduplicated = IncidentService(session, ingest.organization_id).ingest_alert(
        alert,
        enqueue_workflow=settings.auto_investigate_incidents,
        actor=ingest.actor,
    )
    return AlertIngestResponse(incident=incident, deduplicated=deduplicated)
