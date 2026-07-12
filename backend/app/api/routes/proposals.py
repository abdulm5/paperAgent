from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.proposals import MitigationProposalDetail, ProposalDecisionRequest
from app.services.incidents import IncidentNotFoundError
from app.services.proposals import (
    ApprovalPolicyError,
    ProposalConflictError,
    ProposalGenerationError,
    ProposalNotFoundError,
    ProposalService,
    build_proposal_service,
)

incident_router = APIRouter(
    prefix="/incidents/{incident_id}/proposals", tags=["mitigation proposals"]
)
proposal_router = APIRouter(prefix="/proposals", tags=["mitigation proposals"])


def get_proposal_service(session: Session = Depends(get_db)) -> ProposalService:
    return build_proposal_service(session)


@incident_router.post(
    "", response_model=MitigationProposalDetail, status_code=status.HTTP_201_CREATED
)
def generate_proposal(
    incident_id: UUID,
    service: ProposalService = Depends(get_proposal_service),
) -> MitigationProposalDetail:
    try:
        return service.generate(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except ProposalGenerationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@incident_router.get("/latest", response_model=MitigationProposalDetail)
def get_latest_proposal(
    incident_id: UUID,
    service: ProposalService = Depends(get_proposal_service),
) -> MitigationProposalDetail:
    try:
        return service.get_latest(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except ProposalNotFoundError as error:
        raise HTTPException(status_code=404, detail="Proposal not found") from error


@proposal_router.post(
    "/{proposal_id}/decisions", response_model=MitigationProposalDetail
)
def decide_proposal(
    proposal_id: UUID,
    request: ProposalDecisionRequest,
    service: ProposalService = Depends(get_proposal_service),
) -> MitigationProposalDetail:
    try:
        return service.decide(proposal_id, request)
    except ProposalNotFoundError as error:
        raise HTTPException(status_code=404, detail="Proposal not found") from error
    except (ProposalConflictError, ApprovalPolicyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
