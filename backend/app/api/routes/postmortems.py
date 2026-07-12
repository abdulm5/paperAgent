from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.domain.postmortems import (
    PostmortemDetail,
    PostmortemFinalizeRequest,
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


def get_postmortem_service(session: Session = Depends(get_db)) -> PostmortemService:
    return build_postmortem_service(session)


@incident_router.post(
    "", response_model=PostmortemDetail, status_code=status.HTTP_201_CREATED
)
def generate_postmortem(
    incident_id: UUID,
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
    request: PostmortemUpdateRequest,
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    try:
        return service.update(postmortem_id, request)
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error
    except (PostmortemConflictError, PostmortemVersionConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@postmortem_router.post(
    "/{postmortem_id}/finalize", response_model=PostmortemDetail
)
def finalize_postmortem(
    postmortem_id: UUID,
    request: PostmortemFinalizeRequest,
    service: PostmortemService = Depends(get_postmortem_service),
) -> PostmortemDetail:
    try:
        return service.finalize(postmortem_id, request)
    except PostmortemNotFoundError as error:
        raise HTTPException(status_code=404, detail="Postmortem not found") from error
    except (PostmortemConflictError, PostmortemVersionConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@postmortem_router.get("/{postmortem_id}/export")
def export_postmortem(
    postmortem_id: UUID,
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
