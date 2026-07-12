import logging
from uuid import UUID

from app.db.session import SessionLocal
from app.services.postmortems import PostmortemGenerationError, build_postmortem_service

logger = logging.getLogger(__name__)


def run_postmortem_task(incident_id: UUID) -> None:
    with SessionLocal() as session:
        try:
            build_postmortem_service(session).generate(incident_id)
        except PostmortemGenerationError:
            logger.exception("Postmortem generation failed for incident %s", incident_id)
