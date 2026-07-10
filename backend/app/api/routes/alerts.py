from fastapi import APIRouter, status

from app.domain.incidents import AlertIngestResponse, AlertPayload, incident_store

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("", response_model=AlertIngestResponse, status_code=status.HTTP_201_CREATED)
def ingest_alert(alert: AlertPayload) -> AlertIngestResponse:
    """Turn a validated monitoring alert into a detected incident."""
    incident, deduplicated = incident_store.ingest(alert)
    return AlertIngestResponse(incident=incident, deduplicated=deduplicated)
