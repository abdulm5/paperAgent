from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_principal, require_permission
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.collaboration import (
    CollaborationDecisionInput,
    CollaborationDecisionRequest,
    CollaborationOutputCreateInput,
    CollaborationOutputDetail,
)
from app.services.collaboration import (
    CollaborationConnectorUnavailableError,
    CollaborationOutputConflictError,
    CollaborationOutputNotFoundError,
    CollaborationProposalConflictError,
    CollaborationService,
    build_collaboration_service,
)

incident_router = APIRouter(
    prefix="/incidents/{incident_id}/collaboration-outputs",
    tags=["collaboration outputs"],
)
output_router = APIRouter(
    prefix="/collaboration-outputs",
    tags=["collaboration outputs"],
)


def get_collaboration_service(
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> CollaborationService:
    return build_collaboration_service(session, principal.organization_id)


@incident_router.post(
    "",
    response_model=list[CollaborationOutputDetail],
    status_code=status.HTTP_201_CREATED,
)
def prepare_collaboration_outputs(
    incident_id: UUID,
    request: CollaborationOutputCreateInput,
    principal: Principal = Depends(
        require_permission(Permission.COLLABORATION_PREPARE)
    ),
    service: CollaborationService = Depends(get_collaboration_service),
) -> list[CollaborationOutputDetail]:
    try:
        return service.prepare(incident_id, request, actor=principal.actor)
    except CollaborationOutputNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident or proposal not found") from error
    except (
        CollaborationProposalConflictError,
        CollaborationConnectorUnavailableError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@incident_router.get("", response_model=list[CollaborationOutputDetail])
def list_collaboration_outputs(
    incident_id: UUID,
    _principal: Principal = Depends(require_permission(Permission.INCIDENTS_READ)),
    service: CollaborationService = Depends(get_collaboration_service),
) -> list[CollaborationOutputDetail]:
    try:
        return service.list_for_incident(incident_id)
    except CollaborationOutputNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error


@output_router.post(
    "/{output_id}/decisions",
    response_model=CollaborationOutputDetail,
)
def decide_collaboration_output(
    output_id: UUID,
    request: CollaborationDecisionInput,
    principal: Principal = Depends(
        require_permission(Permission.COLLABORATION_DECIDE)
    ),
    service: CollaborationService = Depends(get_collaboration_service),
) -> CollaborationOutputDetail:
    trusted_request = CollaborationDecisionRequest(
        **request.model_dump(),
        actor=principal.actor,
    )
    try:
        return service.decide(output_id, trusted_request)
    except CollaborationOutputNotFoundError as error:
        raise HTTPException(status_code=404, detail="Collaboration output not found") from error
    except (
        CollaborationOutputConflictError,
        CollaborationConnectorUnavailableError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
