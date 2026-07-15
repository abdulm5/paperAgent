from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from app.auth.tokens import is_canonical_jwt
from app.core.config import Settings, settings


class OidcConfigurationError(RuntimeError):
    pass


class InvalidOidcTokenError(ValueError):
    pass


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    email: str
    display_name: str


class OidcVerifier:
    """Validate an OIDC ID token against one configured issuer and JWKS."""

    def __init__(self, config: Settings = settings) -> None:
        if not config.oidc_issuer or not config.oidc_audience or not config.oidc_jwks_url:
            raise OidcConfigurationError("OIDC issuer, audience, and JWKS URL are required")
        self.issuer = config.oidc_issuer
        self.audience = config.oidc_audience
        self.jwks = PyJWKClient(config.oidc_jwks_url)

    def verify(self, encoded: str) -> OidcIdentity:
        if not is_canonical_jwt(encoded):
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(encoded)
            claims: dict[str, Any] = jwt.decode(
                encoded,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["iss", "aud", "sub", "iat", "exp"]},
            )
            email = claims.get("email")
            if not isinstance(email, str) or not email:
                raise InvalidOidcTokenError("OIDC token is missing an email claim")
            display_name = claims.get("name") or email
            if not isinstance(display_name, str):
                raise InvalidOidcTokenError("OIDC name claim must be a string")
            return OidcIdentity(
                issuer=claims["iss"],
                subject=claims["sub"],
                email=email,
                display_name=display_name,
            )
        except (InvalidTokenError, PyJWKClientError, KeyError, TypeError) as error:
            raise InvalidOidcTokenError("Invalid OIDC identity token") from error
