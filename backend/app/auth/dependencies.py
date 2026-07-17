from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.constants import CSRF_HEADER_NAME
from app.auth.service import AuthService, PrincipalNotFoundError
from app.auth.tokens import InvalidSessionTokenError, SessionTokenClaims, decode_session_token
from app.core.config import settings
from app.db.models import AuthSessionRecord, OrganizationRecord
from app.db.session import get_db
from app.domain.auth import IngestContext, Permission, Principal

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class AuthenticatedRequest:
    principal: Principal
    claims: SessionTokenClaims
    via_cookie: bool


def _unauthorized(detail: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive values and production timestamptz values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def get_authenticated_request(
    request: Request,
    session: Session = Depends(get_db),
) -> AuthenticatedRequest:
    authorization = request.headers.get("Authorization")
    via_cookie = authorization is None
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise _unauthorized("Malformed Authorization header")
    else:
        token = request.cookies.get(settings.session_cookie_name, "")
    if not token:
        raise _unauthorized()

    try:
        claims = decode_session_token(token)
    except InvalidSessionTokenError as error:
        raise _unauthorized("Invalid or expired session") from error

    now = datetime.now(UTC)
    session_record = session.scalar(
        select(AuthSessionRecord).where(
            AuthSessionRecord.id == claims.session_id,
            AuthSessionRecord.user_id == claims.user_id,
            AuthSessionRecord.organization_id == claims.organization_id,
        )
    )
    if (
        session_record is None
        or session_record.revoked_at is not None
        or _as_utc(session_record.expires_at) <= now
    ):
        raise _unauthorized("Invalid or expired session")
    try:
        principal = AuthService(session).load_principal(
            claims.user_id,
            claims.organization_id,
        )
    except PrincipalNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "membership_inactive",
                "message": "Session membership is inactive or unavailable",
            },
        ) from error

    if via_cookie and request.method.upper() not in SAFE_METHODS:
        supplied_csrf = request.headers.get(CSRF_HEADER_NAME, "")
        if not supplied_csrf or not compare_digest(supplied_csrf, claims.csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing or invalid CSRF token",
            )
    return AuthenticatedRequest(principal=principal, claims=claims, via_cookie=via_cookie)


def get_current_principal(
    authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
) -> Principal:
    return authenticated.principal


def require_permission(permission: Permission) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if permission not in principal.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission.value}",
            )
        return principal

    return dependency


def require_authenticated_permission(
    permission: Permission,
) -> Callable[..., AuthenticatedRequest]:
    """Return claims and principal together for long-lived authenticated transports."""

    def dependency(
        authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
    ) -> AuthenticatedRequest:
        if permission not in authenticated.principal.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission.value}",
            )
        return authenticated

    return dependency


def require_ingest_context(
    request: Request,
    session: Session = Depends(get_db),
) -> IngestContext:
    supplied_key = request.headers.get("X-PagerAgent-Ingest-Key", "")
    configured_key = settings.ingest_api_key.get_secret_value()
    if not supplied_key or not compare_digest(supplied_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid ingest API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    organization = session.scalar(
        select(OrganizationRecord).where(
            OrganizationRecord.slug == settings.ingest_organization_slug
        )
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingest organization is not configured",
        )
    return IngestContext(
        organization_id=organization.id,
        organization_slug=organization.slug,
    )
