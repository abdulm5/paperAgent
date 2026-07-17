import json
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64DecodeError
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from secrets import token_bytes, token_urlsafe
from typing import Any
from unicodedata import category
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jwt import InvalidTokenError, PyJWK, PyJWKError

from app.auth.tokens import is_canonical_jwt
from app.core.config import Settings, settings

MAX_ID_TOKEN_BYTES = 32_768
MAX_JWKS_KEYS = 100
OIDC_TRANSACTION_AAD_PREFIX = b"pageragent-oidc-transaction-v1\x00"


class OidcConfigurationError(RuntimeError):
    pass


class InvalidOidcTokenError(ValueError):
    pass


class OidcUpstreamError(RuntimeError):
    pass


class InvalidOidcTransactionError(ValueError):
    pass


@dataclass(frozen=True)
class OidcIdentity:
    issuer: str
    subject: str
    email: str
    display_name: str


@dataclass(frozen=True)
class OidcAuthorizationRequest:
    authorization_url: str
    state: str
    state_hash: str
    nonce_hash: str
    browser_binding: str
    browser_binding_hash: str
    code_verifier: str


def digest_oidc_secret(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _base64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _required(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise OidcConfigurationError(f"{name} is required")
    return value


def _contains_control_characters(value: str) -> bool:
    return any(category(character) == "Cc" for character in value)


def build_authorization_request(
    *,
    config: Settings = settings,
) -> OidcAuthorizationRequest:
    authorization_url = _required(config.oidc_authorization_url, "OIDC authorization URL")
    client_id = _required(config.oidc_client_id, "OIDC client ID")
    redirect_uri = _required(config.oidc_redirect_uri, "OIDC redirect URI")

    # These values are deliberately generated independently. The browser only receives
    # the opaque binding and the provider only receives state/nonce/PKCE material.
    state = token_urlsafe(32)
    nonce = token_urlsafe(32)
    browser_binding = token_urlsafe(32)
    code_verifier = token_urlsafe(64)
    code_challenge = _base64url(sha256(code_verifier.encode("ascii")).digest())
    query = urlencode(
        {
            "response_type": "code",
            "scope": "openid email profile",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in authorization_url else "?"
    return OidcAuthorizationRequest(
        authorization_url=f"{authorization_url}{separator}{query}",
        state=state,
        state_hash=digest_oidc_secret(state),
        nonce_hash=digest_oidc_secret(nonce),
        browser_binding=browser_binding,
        browser_binding_hash=digest_oidc_secret(browser_binding),
        code_verifier=code_verifier,
    )


def _transaction_key(config: Settings) -> bytes:
    try:
        key = b64decode(config.oidc_transaction_key.get_secret_value(), validate=True)
    except (Base64DecodeError, ValueError) as error:
        raise OidcConfigurationError("OIDC transaction key is invalid") from error
    if len(key) != 32:
        raise OidcConfigurationError("OIDC transaction key is invalid")
    return key


def _transaction_aad(state_hash: str, nonce_hash: str, organization_slug: str) -> bytes:
    values = (state_hash, nonce_hash, organization_slug)
    if any("\x00" in value for value in values):
        raise InvalidOidcTransactionError("Invalid OIDC transaction")
    return OIDC_TRANSACTION_AAD_PREFIX + "\x00".join(values).encode("utf-8")


def seal_code_verifier(
    code_verifier: str,
    *,
    state_hash: str,
    nonce_hash: str,
    organization_slug: str,
    config: Settings = settings,
) -> tuple[bytes, bytes]:
    if not 43 <= len(code_verifier) <= 128 or not code_verifier.isascii():
        raise InvalidOidcTransactionError("Invalid OIDC transaction")
    nonce = token_bytes(12)
    ciphertext = AESGCM(_transaction_key(config)).encrypt(
        nonce,
        code_verifier.encode("ascii"),
        _transaction_aad(state_hash, nonce_hash, organization_slug),
    )
    return ciphertext, nonce


def open_code_verifier(
    ciphertext: bytes,
    nonce: bytes,
    *,
    state_hash: str,
    nonce_hash: str,
    organization_slug: str,
    config: Settings = settings,
) -> str:
    if len(nonce) != 12 or not 59 <= len(ciphertext) <= 272:
        raise InvalidOidcTransactionError("Invalid OIDC transaction")
    try:
        plaintext = AESGCM(_transaction_key(config)).decrypt(
            nonce,
            ciphertext,
            _transaction_aad(state_hash, nonce_hash, organization_slug),
        )
        code_verifier = plaintext.decode("ascii")
    except (InvalidTag, UnicodeDecodeError) as error:
        raise InvalidOidcTransactionError("Invalid OIDC transaction") from error
    if not 43 <= len(code_verifier) <= 128:
        raise InvalidOidcTransactionError("Invalid OIDC transaction")
    return code_verifier


def _reject_duplicate_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _bounded_json_response(response: httpx.Response, maximum_bytes: int) -> dict[str, Any]:
    length_header = response.headers.get("content-length")
    if length_header is not None:
        try:
            content_length = int(length_header)
            if content_length < 0 or content_length > maximum_bytes:
                raise OidcUpstreamError("OIDC response exceeded the configured limit")
        except ValueError as error:
            raise OidcUpstreamError("OIDC response was invalid") from error

    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > maximum_bytes:
            raise OidcUpstreamError("OIDC response exceeded the configured limit")
        body.extend(chunk)
    if response.status_code != httpx.codes.OK:
        raise OidcUpstreamError("OIDC provider rejected the request")
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in {"application/json", "application/jwk-set+json"}:
        raise OidcUpstreamError("OIDC provider returned an invalid response")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_members,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OidcUpstreamError("OIDC provider returned an invalid response") from error
    if not isinstance(payload, dict):
        raise OidcUpstreamError("OIDC provider returned an invalid response")
    return payload


class OidcTokenClient:
    """Exchange an authorization code at one fixed, bounded token endpoint."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token_url = _required(config.oidc_token_url, "OIDC token URL")
        self.client_id = _required(config.oidc_client_id, "OIDC client ID")
        if config.oidc_client_secret is None:
            raise OidcConfigurationError("OIDC client secret is required")
        self.client_secret = config.oidc_client_secret.get_secret_value()
        self.redirect_uri = _required(config.oidc_redirect_uri, "OIDC redirect URI")
        self.timeout = config.oidc_http_timeout_seconds
        self.maximum_bytes = config.oidc_max_response_bytes
        self.transport = transport

    def exchange(self, code: str, code_verifier: str) -> str:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    self.token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                        "code_verifier": code_verifier,
                    },
                    auth=httpx.BasicAuth(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                ) as response:
                    payload = _bounded_json_response(response, self.maximum_bytes)
        except OidcUpstreamError:
            raise
        except httpx.HTTPError as error:
            raise OidcUpstreamError("OIDC provider was unavailable") from error
        encoded = payload.get("id_token")
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= MAX_ID_TOKEN_BYTES:
            raise OidcUpstreamError("OIDC provider returned an invalid response")
        return encoded


class OidcVerifier:
    """Validate an OIDC ID token against one fixed issuer, client, and bounded JWKS."""

    def __init__(
        self,
        config: Settings = settings,
        *,
        jwks_document: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.issuer = _required(config.oidc_issuer, "OIDC issuer")
        self.client_id = _required(config.oidc_client_id, "OIDC client ID")
        audience = _required(config.oidc_audience, "OIDC audience")
        if audience != self.client_id:
            raise OidcConfigurationError("OIDC audience must equal the client ID")
        self.jwks_url = _required(config.oidc_jwks_url, "OIDC JWKS URL")
        self.maximum_bytes = config.oidc_max_response_bytes
        self.timeout = config.oidc_http_timeout_seconds
        self.jwks_document = jwks_document
        self.transport = transport

    def _jwks(self) -> dict[str, Any]:
        if self.jwks_document is not None:
            return self.jwks_document
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "GET",
                    self.jwks_url,
                    headers={"Accept": "application/jwk-set+json, application/json"},
                ) as response:
                    return _bounded_json_response(response, self.maximum_bytes)
        except OidcUpstreamError:
            raise
        except httpx.HTTPError as error:
            raise OidcUpstreamError("OIDC provider was unavailable") from error

    def _signing_key(self, encoded: str) -> Any:
        try:
            header = jwt.get_unverified_header(encoded)
        except InvalidTokenError as error:
            raise InvalidOidcTokenError("Invalid OIDC identity token") from error
        if header.get("alg") != "RS256":
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not 1 <= len(key_id) <= 256:
            raise InvalidOidcTokenError("Invalid OIDC identity token")

        keys = self._jwks().get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_JWKS_KEYS:
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        matches = [item for item in keys if isinstance(item, dict) and item.get("kid") == key_id]
        if len(matches) != 1:
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        raw_key = matches[0]
        if (
            raw_key.get("kty") != "RSA"
            or raw_key.get("use") not in {None, "sig"}
            or raw_key.get("alg") not in {None, "RS256"}
        ):
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        key_operations = raw_key.get("key_ops")
        if key_operations is not None and (
            not isinstance(key_operations, list) or "verify" not in key_operations
        ):
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        try:
            signing_key = PyJWK.from_dict(raw_key, algorithm="RS256").key
        except (InvalidTokenError, PyJWKError, KeyError, TypeError, ValueError) as error:
            raise InvalidOidcTokenError("Invalid OIDC identity token") from error
        key_size = getattr(signing_key, "key_size", None)
        if not isinstance(key_size, int) or key_size < 2_048:
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        return signing_key

    def verify(
        self,
        encoded: str,
        *,
        expected_nonce_hash: str | None = None,
    ) -> OidcIdentity:
        if len(encoded) > MAX_ID_TOKEN_BYTES or not is_canonical_jwt(encoded):
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        try:
            claims: dict[str, Any] = jwt.decode(
                encoded,
                self._signing_key(encoded),
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={
                    "require": ["iss", "aud", "sub", "iat", "exp", "email"],
                },
            )
        except OidcUpstreamError:
            raise
        except InvalidOidcTokenError:
            raise
        except (InvalidTokenError, KeyError, TypeError, ValueError) as error:
            raise InvalidOidcTokenError("Invalid OIDC identity token") from error

        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if isinstance(audience, list):
            if (
                not audience
                or any(not isinstance(item, str) for item in audience)
                or len(set(audience)) != len(audience)
                or self.client_id not in audience
                or (len(audience) > 1 and authorized_party != self.client_id)
            ):
                raise InvalidOidcTokenError("Invalid OIDC identity token")
        elif audience != self.client_id:
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        if authorized_party is not None and authorized_party != self.client_id:
            raise InvalidOidcTokenError("Invalid OIDC identity token")

        if expected_nonce_hash is not None:
            nonce = claims.get("nonce")
            if (
                not isinstance(nonce, str)
                or not nonce.isascii()
                or not compare_digest(digest_oidc_secret(nonce), expected_nonce_hash)
            ):
                raise InvalidOidcTokenError("Invalid OIDC identity token")

        issuer = claims.get("iss")
        subject = claims.get("sub")
        email = claims.get("email")
        email_verified = claims.get("email_verified")
        display_name = claims.get("name") or email
        if (
            issuer != self.issuer
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 500
            or _contains_control_characters(subject)
            or not isinstance(email, str)
            or not 1 <= len(email) <= 320
            or _contains_control_characters(email)
            or email_verified is not True
            or not isinstance(display_name, str)
            or not 1 <= len(display_name) <= 200
            or _contains_control_characters(display_name)
        ):
            raise InvalidOidcTokenError("Invalid OIDC identity token")
        return OidcIdentity(
            issuer=issuer,
            subject=subject,
            email=email,
            display_name=display_name,
        )
