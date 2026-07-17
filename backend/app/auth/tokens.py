from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_urlsafe
from uuid import UUID

import jwt
from jwt import InvalidTokenError

from app.auth.constants import (
    INTERNAL_TOKEN_AUDIENCE,
    INTERNAL_TOKEN_ISSUER,
    INTERNAL_TOKEN_TYPE,
)
from app.core.config import Settings, settings


class InvalidSessionTokenError(ValueError):
    pass


@dataclass(frozen=True)
class SessionTokenClaims:
    session_id: UUID
    user_id: UUID
    organization_id: UUID
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedSessionToken:
    encoded: str
    csrf_token: str
    session_id: UUID
    expires_at: datetime


def is_canonical_jwt(encoded: str) -> bool:
    """Reject alternate base64url spellings of otherwise identical JWT bytes."""
    segments = encoded.split(".")
    if len(segments) != 3 or any(not segment or "=" in segment for segment in segments):
        return False
    try:
        return all(
            urlsafe_b64encode(urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            .rstrip(b"=")
            .decode("ascii")
            == segment
            for segment in segments
        )
    except (BinasciiError, ValueError, UnicodeError):
        return False


def issue_session_token(
    user_id: UUID,
    organization_id: UUID,
    *,
    session_id: UUID,
    expires_at: datetime,
    config: Settings = settings,
) -> IssuedSessionToken:
    now = datetime.now(UTC).replace(microsecond=0)
    if expires_at.tzinfo is None:
        raise ValueError("Session expiration must include a timezone")
    normalized_expiry = expires_at.astimezone(UTC).replace(microsecond=0)
    if normalized_expiry <= now:
        raise ValueError("Session expiration must be in the future")
    csrf_token = token_urlsafe(32)
    claims = {
        "iss": INTERNAL_TOKEN_ISSUER,
        "aud": INTERNAL_TOKEN_AUDIENCE,
        "sub": str(user_id),
        "org": str(organization_id),
        "csrf": csrf_token,
        "typ": INTERNAL_TOKEN_TYPE,
        "jti": str(session_id),
        "iat": now,
        "nbf": now,
        "exp": normalized_expiry,
    }
    encoded = jwt.encode(
        claims,
        config.session_secret.get_secret_value(),
        algorithm="HS256",
    )
    return IssuedSessionToken(
        encoded=encoded,
        csrf_token=csrf_token,
        session_id=session_id,
        expires_at=normalized_expiry,
    )


def decode_session_token(
    encoded: str,
    *,
    config: Settings = settings,
) -> SessionTokenClaims:
    if not is_canonical_jwt(encoded):
        raise InvalidSessionTokenError("Invalid or expired session")
    try:
        claims = jwt.decode(
            encoded,
            config.session_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=INTERNAL_TOKEN_AUDIENCE,
            issuer=INTERNAL_TOKEN_ISSUER,
            options={
                "require": [
                    "iss",
                    "aud",
                    "sub",
                    "org",
                    "csrf",
                    "typ",
                    "jti",
                    "iat",
                    "nbf",
                    "exp",
                ],
            },
        )
        if claims["typ"] != INTERNAL_TOKEN_TYPE:
            raise InvalidSessionTokenError("Unexpected token type")
        csrf_token = claims["csrf"]
        if not isinstance(csrf_token, str) or len(csrf_token) < 32:
            raise InvalidSessionTokenError("Invalid CSRF claim")
        session_id = UUID(claims["jti"])
        return SessionTokenClaims(
            session_id=session_id,
            user_id=UUID(claims["sub"]),
            organization_id=UUID(claims["org"]),
            csrf_token=csrf_token,
            expires_at=datetime.fromtimestamp(float(claims["exp"]), UTC),
        )
    except (InvalidTokenError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        if isinstance(error, InvalidSessionTokenError):
            raise
        raise InvalidSessionTokenError("Invalid or expired session") from error
