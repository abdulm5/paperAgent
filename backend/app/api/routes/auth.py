from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthenticatedRequest, get_authenticated_request
from app.auth.oidc import (
    InvalidOidcTokenError,
    OidcConfigurationError,
    OidcVerifier,
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


def _issue_session(
    response: Response,
    service: AuthService,
    user_id,
    organization_id,
) -> tuple[SessionResponse, str]:
    token = issue_session_token(user_id, organization_id)
    principal = service.load_principal(user_id, organization_id)
    _set_session_cookie(response, token.encoded)
    return service.build_session(principal, token.csrf_token), token.encoded


def _development_only() -> None:
    if settings.environment not in {"local", "test"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


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
    )
    return SessionTokenResponse(session=session_response, access_token=encoded)


@router.get("/session", response_model=SessionResponse)
def get_session(
    authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
    session: Session = Depends(get_db),
) -> SessionResponse:
    return AuthService(session).build_session(
        authenticated.principal,
        authenticated.claims.csrf_token,
    )


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
    except PrincipalNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active membership in the requested organization",
        ) from error
    session_response, _ = _issue_session(
        response,
        service,
        principal.user_id,
        principal.organization_id,
    )
    return session_response


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    response: Response,
    _authenticated: AuthenticatedRequest = Depends(get_authenticated_request),
) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        secure=_secure_cookie(),
        httponly=True,
        samesite="strict",
        path="/",
    )


@router.post("/oidc/exchange", response_model=SessionResponse)
def exchange_oidc_session(
    request_body: OidcExchangeRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_db),
) -> SessionResponse:
    if settings.auth_mode != "oidc":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not encoded:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC identity token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        identity = OidcVerifier().verify(encoded)
    except OidcConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC is not configured",
        ) from error
    except InvalidOidcTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OIDC identity token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    service = AuthService(session)
    try:
        principal = service.load_oidc_principal(identity, request_body.organization_id)
    except IdentityNotProvisionedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error
    session_response, _ = _issue_session(
        response,
        service,
        principal.user_id,
        principal.organization_id,
    )
    return session_response
