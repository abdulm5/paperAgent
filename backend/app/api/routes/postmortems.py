from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_principal, require_permission
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.postmortems import (
    PostmortemDetail,
    PostmortemFinalizeInput,
    PostmortemFinalizeRequest,
    PostmortemUpdateInput,
    PostmortemUpdateRequest,
)
from app.services.incidents import IncidentNotFoundError
from app.services.postmortems import (
    PostmortemConflictError,
    PostmortemGenerationError,
    PostmortemNotFoundError,
    PostmortemService,
    PostmortemVersionConflictError,
    build_postmortem_service,
)

incident_router = APIRouter(
    prefix="/incidents/{incident_id}/postmortem", tags=["postmortems"]
)
postmortem_router = APIRouter(prefix="/postmortems", tags=["postmortems"])


def get_postmortem_service(
    session: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> PostmortemService:
    return build_postmortem_service(session, principal.organization_id)


@incident_router.post(
    "", response_model=PostmortemDetail, status_code=status.HTTP_201_CREATED
)
def generate_postmortem(
    incident_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.POSTMORTEMS_GENERATE)
    ),
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    try:
        return service.generate(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except PostmortemGenerationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@incident_router.get("", response_model=PostmortemDetail)
def get_incident_postmortem(
    incident_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.INCIDENTS_READ)
    ),
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    try:
        return service.get_for_incident(incident_id)
    except IncidentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Incident not found") from error
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error


@postmortem_router.put("/{postmortem_id}", response_model=PostmortemDetail)
def update_postmortem(
    postmortem_id: UUID,
    request: PostmortemUpdateInput,
    principal: Principal = Depends(
        require_permission(Permission.POSTMORTEMS_EDIT)
    ),
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    trusted_request = PostmortemUpdateRequest(
        **request.model_dump(),
        actor=principal.actor,
    )
    try:
        return service.update(postmortem_id, trusted_request)
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error
    except (PostmortemConflictError, PostmortemVersionConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@postmortem_router.post(
    "/{postmortem_id}/finalize", response_model=PostmortemDetail
)
def finalize_postmortem(
    postmortem_id: UUID,
    request: PostmortemFinalizeInput,
    principal: Principal = Depends(
        require_permission(Permission.POSTMORTEMS_FINALIZE)
    ),
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    trusted_request = PostmortemFinalizeRequest(
        **request.model_dump(),
        actor=principal.actor,
    )
    try:
        return service.finalize(postmortem_id, trusted_request)
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error
    except (PostmortemConflictError, PostmortemVersionConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@postmortem_router.get("/{postmortem_id}/export")
def export_postmortem(
    postmortem_id: UUID,
    _principal: Principal = Depends(
        require_permission(Permission.INCIDENTS_READ)
    ),
    service: PostmortemService = Depends(get_postmortem_service),
) -> Response:
    try:
        markdown = service.export_markdown(postmortem_id)
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="pageragent-{postmortem_id}.md"'
        },
    )
