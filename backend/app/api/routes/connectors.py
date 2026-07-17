from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.dependencies import require_permission
from app.connectors.contracts import ConnectorContractError
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.connectors import (
    ConnectorAuditEvent,
    ConnectorCreateInput,
    ConnectorCredentialsInput,
    ConnectorPatchInput,
    ConnectorSummary,
    ConnectorValidateInput,
)
from app.services.connectors import (
    ConnectorAuditIntegrityError,
    ConnectorAuthorityChangedError,
    ConnectorCustodyUnavailableError,
    ConnectorEnablementError,
    ConnectorNameConflictError,
    ConnectorNotFoundError,
    ConnectorService,
    ConnectorVersionConflictError,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("", response_model=list[ConnectorSummary])
def list_connectors(
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_READ)),
    session: Session = Depends(get_db),
) -> list[ConnectorSummary]:
    return ConnectorService(session, principal.organization_id).list_connectors()


@router.get("/{connector_id}", response_model=ConnectorSummary)
def get_connector(
    connector_id: UUID,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_READ)),
    session: Session = Depends(get_db),
) -> ConnectorSummary:
    try:
        return ConnectorService(session, principal.organization_id).get_connector(connector_id)
    except ConnectorNotFoundError as error:
        raise _not_found() from error


@router.get("/{connector_id}/events", response_model=list[ConnectorAuditEvent])
def list_connector_events(
    connector_id: UUID,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_READ)),
    session: Session = Depends(get_db),
) -> list[ConnectorAuditEvent]:
    try:
        return ConnectorService(session, principal.organization_id).list_events(connector_id)
    except ConnectorNotFoundError as error:
        raise _not_found() from error
    except ConnectorAuditIntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Connector audit ledger integrity check failed",
        ) from error


@router.post("", response_model=ConnectorSummary, status_code=status.HTTP_201_CREATED)
def create_connector(
    request: ConnectorCreateInput,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_MANAGE)),
    session: Session = Depends(get_db),
) -> ConnectorSummary:
    try:
        return ConnectorService(session, principal.organization_id).create_connector(
            request,
            actor=principal.actor,
            actor_user_id=principal.user_id,
        )
    except ConnectorNameConflictError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConnectorEnablementError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConnectorContractError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    except ConnectorCustodyUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    except ConnectorAuthorityChangedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_inactive", "message": str(error)},
        ) from error


@router.patch("/{connector_id}", response_model=ConnectorSummary)
def patch_connector(
    connector_id: UUID,
    request: ConnectorPatchInput,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_MANAGE)),
    session: Session = Depends(get_db),
) -> ConnectorSummary:
    try:
        return ConnectorService(session, principal.organization_id).patch_connector(
            connector_id,
            request,
            actor=principal.actor,
        )
    except ConnectorNotFoundError as error:
        raise _not_found() from error
    except (ConnectorNameConflictError, ConnectorEnablementError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConnectorVersionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(error), "current_version": error.current_version},
        ) from error
    except ConnectorContractError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.put("/{connector_id}/credentials", response_model=ConnectorSummary)
def put_connector_credentials(
    connector_id: UUID,
    request: ConnectorCredentialsInput,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_MANAGE)),
    session: Session = Depends(get_db),
) -> ConnectorSummary:
    try:
        return ConnectorService(session, principal.organization_id).put_credentials(
            connector_id,
            request,
            actor=principal.actor,
            actor_user_id=principal.user_id,
        )
    except ConnectorNotFoundError as error:
        raise _not_found() from error
    except ConnectorVersionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(error), "current_version": error.current_version},
        ) from error
    except ConnectorContractError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    except ConnectorCustodyUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    except ConnectorAuthorityChangedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_inactive", "message": str(error)},
        ) from error


@router.post("/{connector_id}/validate", response_model=ConnectorSummary)
def validate_connector(
    connector_id: UUID,
    request: ConnectorValidateInput,
    principal: Principal = Depends(require_permission(Permission.CONNECTORS_VALIDATE)),
    session: Session = Depends(get_db),
) -> ConnectorSummary:
    try:
        return ConnectorService(session, principal.organization_id).validate_connector(
            connector_id,
            request.expected_version,
            actor=principal.actor,
            actor_user_id=principal.user_id,
        )
    except ConnectorNotFoundError as error:
        raise _not_found() from error
    except ConnectorVersionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(error), "current_version": error.current_version},
        ) from error
    except ConnectorCustodyUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
            headers={"Retry-After": "5"},
        ) from error
    except ConnectorAuthorityChangedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "membership_inactive", "message": str(error)},
        ) from error


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
