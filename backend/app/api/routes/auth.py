from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedRequest, get_authenticated_request
from app.auth.oidc import (
    InvalidOidcTokenError,
    InvalidOidcTransactionError,
    OidcConfigurationError,
    OidcTokenClient,
    OidcUpstreamError,
    OidcVerifier,
    build_authorization_request,
    digest_oidc_secret,
    open_code_verifier,
    seal_code_verifier,
)
from app.auth.service import (
    DEV_PERSONAS,
    AuthService,
    IdentityNotProvisionedError,
    OrganizationNotFoundError,
    PersonaNotFoundError,
    PrincipalNotFoundError,
)
from app.auth.tokens import issue_session_token
from app.core.config import settings
from app.db.models import (
    AuthSessionRecord,
    OidcLoginTransactionRecord,
    OrganizationMembershipRecord,
    OrganizationRecord,
    UserRecord,
)
from app.db.session import get_db
from app.domain.auth import (
    DevPersonasResponse,
    DevSessionRequest,
    OidcExchangeRequest,
    SessionResponse,
    SessionTokenResponse,
    SwitchOrganizationRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

OIDC_CALLBACK_PATH = "/api/v1/auth/oidc/callback"
OIDC_CALLBACK_MAX_QUERY_BYTES = 4_096
OIDC_CALLBACK_MAX_EXTENSION_PARAMETERS = 16
OIDC_CALLBACK_MAX_EXTENSION_NAME_LENGTH = 64
OIDC_CALLBACK_MAX_EXTENSION_VALUE_LENGTH = 1_024
OIDC_CALLBACK_EXTENSION_LIMITS = {
    "error_uri": 2_048,
    "session_state": 512,
}
OIDC_MAX_PENDING_LOGIN_TRANSACTIONS_PER_ORGANIZATION = 1_000
OIDC_CALLBACK_LIMITS = {
    "code": 2_048,
    "state": 256,
    "error": 128,
    "error_description": 512,
    "iss": 500,
}


def _secure_cookie() -> bool:
    return settings.session_cookie_secure or settings.environment not in {"local", "test"}


def _set_session_cookie(response: Response, encoded: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=encoded,
        max_age=settings.session_ttl_seconds,
        secure=_secure_cookie(),
        httponly=True,
        samesite="strict",
        path="/",
    )


def _set_oidc_login_cookie(response: Response, browser_binding: str) -> None:
    response.set_cookie(
        key=settings.oidc_login_cookie_name,
        value=browser_binding,
        max_age=settings.oidc_login_ttl_seconds,
        secure=True,
        httponly=True,
        samesite="lax",
        # Production uses the __Host- cookie prefix, which requires Path=/ and
        # prevents sibling subdomains from setting a competing binding cookie.
        path="/",
    )


def _clear_oidc_login_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.oidc_login_cookie_name,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


def _issue_session(
    response: Response,
    service: AuthService,
    user_id: UUID,
    organization_id: UUID,
    *,
    auth_method: str | None,
    revoke_session_id: UUID | None = None,
    revoke_organization_id: UUID | None = None,
) -> tuple[SessionResponse, str]:
    now = datetime.now(UTC).replace(microsecond=0)
    expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
    session_id = uuid4()

    # Serialize issuance with membership deactivation. Organization locks are
    # acquired in stable order so an org switch cannot invert the lock order of
    # two concurrent membership/session operations.
    authority_organizations = {organization_id}
    if revoke_organization_id is not None:
        authority_organizations.add(revoke_organization_id)
    locked_organizations = set(
        service.session.scalars(
            select(OrganizationRecord.id)
            .where(OrganizationRecord.id.in_(authority_organizations))
            .order_by(OrganizationRecord.id)
            .with_for_update()
        ).all()
    )
    if locked_organizations != authority_organizations:
        raise PrincipalNotFoundError("Session organization is unavailable")
    active_membership = service.session.scalar(
        select(OrganizationMembershipRecord)
        .join(UserRecord, UserRecord.id == OrganizationMembershipRecord.user_id)
        .where(
            OrganizationMembershipRecord.organization_id == organization_id,
            OrganizationMembershipRecord.user_id == user_id,
            OrganizationMembershipRecord.is_active.is_(True),
            UserRecord.is_active.is_(True),
        )
        .with_for_update()
    )
    if active_membership is None:
        raise PrincipalNotFoundError("Session membership is inactive or unavailable")

    if revoke_session_id is not None:
        current = service.session.scalar(
            select(AuthSessionRecord)
            .where(
                AuthSessionRecord.id == revoke_session_id,
                AuthSessionRecord.user_id == user_id,
                AuthSessionRecord.organization_id == revoke_organization_id,
                AuthSessionRecord.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if current is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
            )
        current.revoked_at = now
        if auth_method is None:
            auth_method = current.auth_method
    if auth_method not in {"local", "oidc"}:
        raise RuntimeError("Unsupported authentication method")

    token = issue_session_token(
        user_id,
        organization_id,
        session_id=session_id,
        expires_at=expires_at,
    )
    principal = service.load_principal(user_id, organization_id)
    service.session.add(
        AuthSessionRecord(
            id=session_id,
            user_id=user_id,
            organization_id=organization_id,
            auth_method=auth_method,
            expires_at=token.expires_at,
        )
    )
    try:
        service.session.flush()
        session_response = service.build_session(principal, token.csrf_token)
        service.session.commit()
    except Exception:
        service.session.rollback()
        raise
    _set_session_cookie(response, token.encoded)
    return session_response, token.encoded


def _development_only() -> None:
    if settings.environment not in {"local", "test"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _oidc_only() -> None:
    if settings.auth_mode != "oidc":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _oidc_failure(status_code: int) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content={"detail": "OIDC sign-in failed"},
    )
    _clear_oidc_login_cookie(response)
    _no_store(response)
    return response


def _callback_parameters(request: Request) -> dict[str, str]:
    if len(request.scope.get("query_string", b"")) > OIDC_CALLBACK_MAX_QUERY_BYTES:
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    values: dict[str, str] = {}
    extension_parameters = 0
    for name, value in request.query_params.multi_items():
        maximum = OIDC_CALLBACK_LIMITS.get(name)
        if maximum is not None:
            if name in values or not value or len(value) > maximum:
                raise InvalidOidcTransactionError("Invalid OIDC callback")
            values[name] = value
            continue

        # OAuth/OIDC response parameters are extensible. Bound their decoded shape,
        # then deliberately discard them so they cannot affect callback semantics or
        # be reflected in an error. The raw query has an independent byte ceiling.
        extension_parameters += 1
        extension_maximum = OIDC_CALLBACK_EXTENSION_LIMITS.get(
            name,
            OIDC_CALLBACK_MAX_EXTENSION_VALUE_LENGTH,
        )
        if (
            extension_parameters > OIDC_CALLBACK_MAX_EXTENSION_PARAMETERS
            or not name
            or len(name) > OIDC_CALLBACK_MAX_EXTENSION_NAME_LENGTH
            or not name.isascii()
            or any(
                not (character.isalnum() or character in "._~-")
                for character in name
            )
            or len(value) > extension_maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise InvalidOidcTransactionError("Invalid OIDC callback")
    if "state" not in values:
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    state = values["state"]
    if (
        not 43 <= len(state) <= 128
        or not state.isascii()
        or any(not (character.isalnum() or character in "_-") for character in state)
    ):
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    has_code = "code" in values
    has_error = "error" in values
    if has_code == has_error:
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    if "error_description" in values and not has_error:
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    if values.get("iss") not in {None, settings.oidc_issuer}:
        raise InvalidOidcTransactionError("Invalid OIDC callback")
    return values


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _build_oidc_token_client() -> OidcTokenClient:
    return OidcTokenClient()


def _build_oidc_verifier() -> OidcVerifier:
    return OidcVerifier()


@router.get("/dev/personas", response_model=DevPersonasResponse)
def list_dev_personas() -> DevPersonasResponse:
    _development_only()
    return DevPersonasResponse(personas=[persona.response() for persona in DEV_PERSONAS])


@router.post("/dev/session", response_model=SessionTokenResponse)
def create_dev_session(
    request: DevSessionRequest,
    response: Response,
    session: Session = Depends(get_db),
) -> SessionTokenResponse:
    _development_only()
    service = AuthService(session)
    try:
        principal = service.create_dev_principal(request.persona, request.organization_slug)
    except PersonaNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except OrganizationNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    session_response, encoded = _issue_session(
        response,
        service,
        principal.user_id,
        principal.organization_id,
        auth_method="local",
    )
    _no_store(response)
    return SessionTokenResponse(session=session_response, access_token=encoded)


@router.get("/session", response_model=SessionResponse)
def get_session(
    response: Response,
    authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
    session: Session = Depends(get_db),
) -> SessionResponse:
    session_response = AuthService(session).build_session(
        authenticated.principal,
        authenticated.claims.csrf_token,
    )
    _no_store(response)
    return session_response


@router.post("/session/switch", response_model=SessionResponse)
def switch_session(
    request: SwitchOrganizationRequest,
    response: Response,
    authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
    session: Session = Depends(get_db),
) -> SessionResponse:
    service = AuthService(session)
    try:
        principal = service.load_principal(
            authenticated.principal.user_id,
            request.organization_id,
        )
        session_response, _ = _issue_session(
            response,
            service,
            principal.user_id,
            principal.organization_id,
            auth_method=None,
            revoke_session_id=authenticated.claims.session_id,
            revoke_organization_id=authenticated.claims.organization_id,
        )
    except PrincipalNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active membership in the requested organization",
        ) from error
    _no_store(response)
    return session_response


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    response: Response,
    authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
    session: Session = Depends(get_db),
) -> None:
    current = session.scalar(
        select(AuthSessionRecord)
        .where(
            AuthSessionRecord.id == authenticated.claims.session_id,
            AuthSessionRecord.user_id == authenticated.claims.user_id,
            AuthSessionRecord.organization_id == authenticated.claims.organization_id,
            AuthSessionRecord.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if current is not None:
        current.revoked_at = datetime.now(UTC)
        session.commit()
    response.delete_cookie(
        key=settings.session_cookie_name,
        secure=_secure_cookie(),
        httponly=True,
        samesite="strict",
        path="/",
    )
    _no_store(response)


@router.get("/oidc/login")
def begin_oidc_login(session: Session = Depends(get_db)) -> Response:
    _oidc_only()
    organization_slug = settings.oidc_default_organization_slug
    if not organization_slug:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured",
        )
    now = datetime.now(UTC).replace(microsecond=0)
    organization = session.scalar(
        select(OrganizationRecord)
        .where(OrganizationRecord.slug == organization_slug)
        .with_for_update()
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC organization is not configured",
        )
    try:
        session.execute(
            delete(OidcLoginTransactionRecord)
            .where(
                OidcLoginTransactionRecord.organization_slug == organization.slug,
                or_(
                    OidcLoginTransactionRecord.consumed_at.is_not(None),
                    OidcLoginTransactionRecord.expires_at <= now,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        pending_transactions = session.scalar(
            select(func.count(OidcLoginTransactionRecord.id)).where(
                OidcLoginTransactionRecord.organization_slug == organization.slug,
                OidcLoginTransactionRecord.consumed_at.is_(None),
                OidcLoginTransactionRecord.expires_at > now,
            )
        )
        if (
            pending_transactions is not None
            and pending_transactions
            >= OIDC_MAX_PENDING_LOGIN_TRANSACTIONS_PER_ORGANIZATION
        ):
            # Commit safe garbage collection before releasing the organization lock.
            session.commit()
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "OIDC sign-in temporarily unavailable"},
                headers={"Retry-After": str(settings.oidc_login_ttl_seconds)},
            )
            _no_store(response)
            return response

        authorization = build_authorization_request()
        ciphertext, verifier_nonce = seal_code_verifier(
            authorization.code_verifier,
            state_hash=authorization.state_hash,
            nonce_hash=authorization.nonce_hash,
            organization_slug=organization.slug,
        )
    except (OidcConfigurationError, InvalidOidcTransactionError) as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured",
        ) from error

    session.add(
        OidcLoginTransactionRecord(
            state_hash=authorization.state_hash,
            browser_binding_hash=authorization.browser_binding_hash,
            nonce_hash=authorization.nonce_hash,
            verifier_ciphertext=ciphertext,
            verifier_nonce=verifier_nonce,
            organization_slug=organization.slug,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.oidc_login_ttl_seconds),
        )
    )
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    response = RedirectResponse(url=authorization.authorization_url, status_code=303)
    _set_oidc_login_cookie(response, authorization.browser_binding)
    _no_store(response)
    return response


@router.get("/oidc/callback")
def complete_oidc_login(
    request: Request,
    session: Session = Depends(get_db),
) -> Response:
    if settings.auth_mode != "oidc":
        return _oidc_failure(status.HTTP_404_NOT_FOUND)
    try:
        parameters = _callback_parameters(request)
        browser_binding = request.cookies.get(settings.oidc_login_cookie_name)
        if (
            browser_binding is None
            or not browser_binding.isascii()
            or not 32 <= len(browser_binding) <= 256
        ):
            raise InvalidOidcTransactionError("Invalid OIDC callback")
        state_hash = digest_oidc_secret(parameters["state"])
        transaction = session.scalar(
            select(OidcLoginTransactionRecord).where(
                OidcLoginTransactionRecord.state_hash == state_hash
            )
        )
        now = datetime.now(UTC).replace(microsecond=0)
        if (
            transaction is None
            or transaction.consumed_at is not None
            or _utc(transaction.expires_at) <= now
            or transaction.organization_slug != settings.oidc_default_organization_slug
            or not compare_digest(
                transaction.browser_binding_hash,
                digest_oidc_secret(browser_binding),
            )
        ):
            raise InvalidOidcTransactionError("Invalid OIDC callback")
        code_verifier = open_code_verifier(
            transaction.verifier_ciphertext,
            transaction.verifier_nonce,
            state_hash=transaction.state_hash,
            nonce_hash=transaction.nonce_hash,
            organization_slug=transaction.organization_slug,
        )
        consumed = session.execute(
            update(OidcLoginTransactionRecord)
            .where(
                OidcLoginTransactionRecord.id == transaction.id,
                OidcLoginTransactionRecord.consumed_at.is_(None),
                OidcLoginTransactionRecord.expires_at > now,
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        if consumed.rowcount != 1:
            session.rollback()
            raise InvalidOidcTransactionError("Invalid OIDC callback")
        nonce_hash = transaction.nonce_hash
        organization_slug = transaction.organization_slug
        session.commit()
    except InvalidOidcTransactionError:
        session.rollback()
        return _oidc_failure(status.HTTP_400_BAD_REQUEST)
    except OidcConfigurationError:
        session.rollback()
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)
    except SQLAlchemyError:
        session.rollback()
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)

    # The one-time transaction is durably consumed before either upstream call.
    if "error" in parameters:
        return _oidc_failure(status.HTTP_401_UNAUTHORIZED)
    try:
        encoded = _build_oidc_token_client().exchange(parameters["code"], code_verifier)
        identity = _build_oidc_verifier().verify(
            encoded,
            expected_nonce_hash=nonce_hash,
        )
    except OidcConfigurationError:
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)
    except OidcUpstreamError:
        return _oidc_failure(status.HTTP_502_BAD_GATEWAY)
    except InvalidOidcTokenError:
        return _oidc_failure(status.HTTP_401_UNAUTHORIZED)

    organization = session.scalar(
        select(OrganizationRecord).where(OrganizationRecord.slug == organization_slug)
    )
    if organization is None:
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)
    service = AuthService(session)
    try:
        principal = service.load_oidc_principal(identity, organization.id)
    except IdentityNotProvisionedError:
        return _oidc_failure(status.HTTP_403_FORBIDDEN)
    except SQLAlchemyError:
        session.rollback()
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)

    frontend_url = settings.oidc_frontend_url
    if not frontend_url:
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)
    response = RedirectResponse(url=frontend_url, status_code=303)
    try:
        _issue_session(
            response,
            service,
            principal.user_id,
            principal.organization_id,
            auth_method="oidc",
        )
    except PrincipalNotFoundError:
        return _oidc_failure(status.HTTP_403_FORBIDDEN)
    except SQLAlchemyError:
        return _oidc_failure(status.HTTP_503_SERVICE_UNAVAILABLE)
    _clear_oidc_login_cookie(response)
    _no_store(response)
    return response


@router.post("/oidc/exchange", response_model=SessionResponse)
def exchange_oidc_session(
    request_body: OidcExchangeRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> SessionResponse:
    _development_only()
    _oidc_only()
    authorization = request.headers.get("Authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not encoded:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC identity token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        identity = _build_oidc_verifier().verify(encoded)
    except OidcConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured",
        ) from error
    except (InvalidOidcTokenError, OidcUpstreamError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OIDC identity token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    service = AuthService(session)
    try:
        principal = service.load_oidc_principal(identity, request_body.organization_id)
    except IdentityNotProvisionedError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OIDC identity is not provisioned",
        ) from error
    session_response, _ = _issue_session(
        response,
        service,
        principal.user_id,
        principal.organization_id,
        auth_method="oidc",
    )
    _no_store(response)
    return session_response
