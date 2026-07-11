from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.domain.incidents import AlertIngestResponse, AlertPayload
from app.services.incidents import IncidentService
from app.tasks.investigations import run_investigation_task

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("", response_model=AlertIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_alert(
    alert: AlertPayload,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> AlertIngestResponse:
    """Turn a validated monitoring alert into a detected incident."""
    incident, deduplicated = IncidentService(session).ingest_alert(alert)
    if settings.auto_investigate_incidents and not deduplicated:
        background_tasks.add_task(run_investigation_task, incident.id)
    return AlertIngestResponse(incident=incident, deduplicated=deduplicated)
