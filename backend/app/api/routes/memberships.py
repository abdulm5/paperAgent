from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth.constants import LOCAL_MEMBERSHIP_ISSUER
from app.auth.dependencies import require_permission
from app.core.config import settings
from app.db.session import get_db
from app.domain.auth import Permission, Principal
from app.domain.memberships import (
    IdentityAuditEvent,
    MembershipDetail,
    MembershipProvisionInput,
    MembershipUpdateInput,
)
from app.memberships.service import (
    ActorAuthorityChangedError,
    MembershipAlreadyExistsError,
    MembershipContractError,
    MembershipIdentityConflictError,
    MembershipInvariantError,
    MembershipNotFoundError,
    MembershipService,
    MembershipVersionConflictError,
)

router = APIRouter(prefix="/memberships", tags=["memberships"])


def _service(session: Session, principal: Principal) -> MembershipService:
    configured_issuer = settings.oidc_issuer
    if not configured_issuer and settings.environment in {"local", "test"}:
        configured_issuer = LOCAL_MEMBERSHIP_ISSUER
    return MembershipService(
        session,
        principal.organization_id,
        configured_issuer=configured_issuer,
    )


@router.get("", response_model=list[MembershipDetail])
def list_memberships(
    principal: Principal = Depends(require_permission(Permission.MEMBERSHIPS_READ)),
    session: Session = Depends(get_db),
) -> list[MembershipDetail]:
    return _service(session, principal).list_memberships()


@router.get("/audit", response_model=list[IdentityAuditEvent])
def list_membership_audit(
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(require_permission(Permission.MEMBERSHIPS_READ)),
    session: Session = Depends(get_db),
) -> list[IdentityAuditEvent]:
    return _service(session, principal).list_audit_events(limit=limit)


@router.post("", response_model=MembershipDetail, status_code=status.HTTP_201_CREATED)
def provision_membership(
    request: MembershipProvisionInput,
    principal: Principal = Depends(require_permission(Permission.MEMBERSHIPS_MANAGE)),
    session: Session = Depends(get_db),
) -> MembershipDetail:
    try:
        return _service(session, principal).provision(
            request,
            actor_user_id=principal.user_id,
        )
    except ActorAuthorityChangedError as error:
        raise _authority_changed(error) from error
    except (MembershipIdentityConflictError, MembershipAlreadyExistsError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "identity_conflict", "message": str(error)},
        ) from error


@router.patch("/{user_id}", response_model=MembershipDetail)
def update_membership(
    user_id: UUID,
    request: MembershipUpdateInput,
    principal: Principal = Depends(require_permission(Permission.MEMBERSHIPS_MANAGE)),
    session: Session = Depends(get_db),
) -> MembershipDetail:
    try:
        return _service(session, principal).update(
            user_id,
            request,
            actor_user_id=principal.user_id,
        )
    except MembershipNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Membership not found",
        ) from error
    except ActorAuthorityChangedError as error:
        raise _authority_changed(error) from error
    except MembershipVersionConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "membership_version_conflict",
                "message": str(error),
                "current_version": error.current_version,
            },
        ) from error
    except MembershipInvariantError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "membership_invariant", "message": str(error)},
        ) from error
    except MembershipContractError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "membership_no_change", "message": str(error)},
        ) from error


def _authority_changed(error: ActorAuthorityChangedError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "membership_inactive", "message": str(error)},
    )
