import logging
from uuid import UUID

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.investigations import InvestigationExecutionError, build_investigation_service
from app.services.proposals import ProposalGenerationError, build_proposal_service

logger = logging.getLogger(__name__)


def run_investigation_task(incident_id: UUID) -> None:
    with SessionLocal() as session:
        try:
            investigation = build_investigation_service(session).run(incident_id)
            if settings.auto_generate_proposals:
                build_proposal_service(session).generate(incident_id, investigation.id)
        except InvestigationExecutionError:
            logger.exception("Investigation failed for incident %s", incident_id)
        except ProposalGenerationError:
            logger.exception("Proposal generation failed for incident %s", incident_id)
