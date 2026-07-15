from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_principal, require_permission
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.proposals import (
    MitigationProposalDetail,
    ProposalDecisionInput,
    ProposalDecisionRequest,
)
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


def get_proposal_service(
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> ProposalService:
    return build_proposal_service(session, principal.organization_id)


@incident_router.post(
    "", response_model=MitigationProposalDetail, status_code=status.HTTP_201_CREATED
)
def generate_proposal(
    incident_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.PROPOSALS_GENERATE)
    ),
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
    _principal: Principal = Depends(
        require_permission(Permission.INCIDENTS_READ)
    ),
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
    request: ProposalDecisionInput,
    principal: Principal = Depends(
        require_permission(Permission.MITIGATIONS_DECIDE)
    ),
    service: ProposalService = Depends(get_proposal_service),
) -> MitigationProposalDetail:
    trusted_request = ProposalDecisionRequest(
        **request.model_dump(),
        actor=principal.actor,
    )
    try:
        return service.decide(proposal_id, trusted_request)
    except ProposalNotFoundError as error:
        raise HTTPException(status_code=404, detail="Proposal not found") from error
    except (ProposalConflictError, ApprovalPolicyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
