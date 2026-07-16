from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.domain.connectors import GithubConfiguration, GithubCredentials

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
type Clock = Callable[[], datetime]

GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_HTML_ORIGIN = "https://github.com"
GITHUB_ISSUE_PROVIDER_VERSION = "github-app-issues-v1"
DEFAULT_GITHUB_API_VERSION = "2026-03-10"

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_API_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN_CONTROL_PATTERN = re.compile(r"[\x00-\x20\x7f]")
_TEXT_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINK_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class GitHubIssuePublisherError(RuntimeError):
    """Sanitized provider failure safe for workflow classification and receipts."""

    code = "github_issue_provider_failure"
    permanent = False
    retryable = False
    ambiguous = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class GitHubIssuePermanentError(GitHubIssuePublisherError):
    code = "github_issue_permanent_failure"
    permanent = True


class GitHubIssueConfigurationError(GitHubIssuePermanentError):
    code = "github_issue_configuration_invalid"


class GitHubIssueAuthenticationError(GitHubIssuePermanentError):
    code = "github_issue_authentication_failed"


class GitHubIssuePermissionError(GitHubIssuePermanentError):
    code = "github_issue_permission_denied"


class GitHubIssueNotFoundError(GitHubIssuePermanentError):
    code = "github_issue_repository_not_found"


class GitHubIssueRedirectError(GitHubIssuePermanentError):
    code = "github_issue_redirect_rejected"


class GitHubIssueRequestTooLargeError(GitHubIssuePermanentError):
    code = "github_issue_request_too_large"


class GitHubIssueResponseTooLargeError(GitHubIssuePermanentError):
    code = "github_issue_response_too_large"


class GitHubIssueRequestBudgetExceededError(GitHubIssuePermanentError):
    code = "github_issue_request_budget_exhausted"


class GitHubIssueProviderResponseError(GitHubIssuePermanentError):
    code = "github_issue_response_invalid"


class GitHubIssueRetryableError(GitHubIssuePublisherError):
    code = "github_issue_transient_failure"
    retryable = True


class GitHubIssueRateLimitError(GitHubIssueRetryableError):
    code = "github_issue_rate_limited"


class GitHubIssueAmbiguousDeliveryError(GitHubIssueRetryableError):
    """The create request may have committed and must be reconciled before retry."""

    code = "github_issue_delivery_ambiguous"
    ambiguous = True


# Compatibility spelling for callers that classify all provider ambiguity errors.
GitHubIssueAmbiguityError = GitHubIssueAmbiguousDeliveryError


class GitHubIssueReconciliationAmbiguityError(GitHubIssuePermanentError):
    """The bounded scan cannot establish a single durable provider receipt."""

    code = "github_issue_reconciliation_ambiguous"
    ambiguous = True


@dataclass(frozen=True)
class GitHubIssueDeliveryReceipt:
    delivery_id: str
    repository: str
    issue_number: int
    issue_url: str
    reconciled: bool

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "delivery_id": self.delivery_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class GitHubIssuePublisherLimits:
    request_budget: int = 5
    timeout_seconds: float = 5.0
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 512 * 1024
    reconciliation_page_size: int = 50
    max_reconciliation_pages: int = 2
    max_title_length: int = 256
    max_body_length: int = 60_000

    def __post_init__(self) -> None:
        if not 1 <= self.request_budget <= 12:
            raise ValueError("GitHub issue request budget must be between 1 and 12")
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ValueError("GitHub issue timeout must be between 0.1 and 30 seconds")
        if not 256 <= self.max_request_bytes <= 1024 * 1024:
            raise ValueError("GitHub issue request byte limit is invalid")
        if not 256 <= self.max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("GitHub issue response byte limit is invalid")
        if not 1 <= self.reconciliation_page_size <= 100:
            raise ValueError("GitHub issue reconciliation page size is invalid")
        if not 1 <= self.max_reconciliation_pages <= 5:
            raise ValueError("GitHub issue reconciliation page limit is invalid")
        if not 1 <= self.max_title_length <= 256:
            raise ValueError("GitHub issue title limit is invalid")
        if not 1 <= self.max_body_length <= 65_000:
            raise ValueError("GitHub issue body limit is invalid")


@dataclass
class _RequestBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise GitHubIssueRequestBudgetExceededError(
                "GitHub issue request budget was exhausted"
            )
        self.remaining -= 1


@dataclass(frozen=True)
class _InstallationToken:
    value: str
    expires_at: datetime


@dataclass(frozen=True)
class _GitHubResponse:
    status_code: int
    payload: JsonValue
    headers: Mapping[str, str]


class _DuplicateKeyError(ValueError):
    pass


class GitHubIssuePublisher:
    """Independent, least-privilege GitHub App adapter for approved issue delivery."""

    version = GITHUB_ISSUE_PROVIDER_VERSION

    def __init__(
        self,
        configuration: GithubConfiguration,
        credentials: GithubCredentials,
        *,
        limits: GitHubIssuePublisherLimits = GitHubIssuePublisherLimits(),
        client: httpx.Client | None = None,
        now: Clock = lambda: datetime.now(UTC),
        api_version: str = DEFAULT_GITHUB_API_VERSION,
    ) -> None:
        if configuration.api_url != GITHUB_API_ORIGIN:
            raise GitHubIssueConfigurationError("GitHub API origin is invalid")
        if configuration.issue_creation_enabled is not True:
            raise GitHubIssueConfigurationError("GitHub issue creation is not enabled")
        if _API_VERSION_PATTERN.fullmatch(api_version) is None:
            raise GitHubIssueConfigurationError("GitHub API version is invalid")

        repository_parts = configuration.repository.split("/")
        if len(repository_parts) != 2:
            raise GitHubIssueConfigurationError("GitHub repository binding is invalid")
        owner, repository_name = repository_parts
        if (
            owner in {".", ".."}
            or repository_name in {".", ".."}
            or _OWNER_PATTERN.fullmatch(owner) is None
            or _REPOSITORY_PATTERN.fullmatch(repository_name) is None
        ):
            raise GitHubIssueConfigurationError("GitHub repository binding is invalid")

        try:
            private_key = serialization.load_pem_private_key(
                credentials.private_key.get_secret_value().encode("utf-8"),
                password=None,
            )
        except (TypeError, ValueError, UnicodeError, UnsupportedAlgorithm):
            raise GitHubIssueConfigurationError("GitHub App private key is invalid") from None
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise GitHubIssueConfigurationError("GitHub App private key must be an RSA key")
        if private_key.key_size < 2_048:
            raise GitHubIssueConfigurationError(
                "GitHub App private key must be at least 2048 bits"
            )

        self.service = configuration.service
        self.owner = owner
        self.repo = repository_name
        self.repository = f"{owner}/{repository_name}"
        self.app_id = configuration.app_id
        self.installation_id = configuration.installation_id
        self.api_version = api_version
        self.limits = limits
        self._private_key = private_key
        self._now = now
        self._request_lock = RLock()
        self._installation_token: _InstallationToken | None = None

        connect_timeout = min(3.0, limits.timeout_seconds)
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=limits.timeout_seconds,
            write=limits.timeout_seconds,
            pool=connect_timeout,
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=GITHUB_API_ORIGIN,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubIssuePublisher:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def build_app_jwt(self) -> str:
        current_time = self._current_time()
        timestamp = int(current_time.timestamp())
        try:
            return jwt.encode(
                {
                    "iat": timestamp - 60,
                    "exp": timestamp + (9 * 60),
                    "iss": str(self.app_id),
                },
                self._private_key,
                algorithm="RS256",
            )
        except (TypeError, ValueError, jwt.PyJWTError):
            raise GitHubIssueAuthenticationError("GitHub App JWT signing failed") from None

    def validate(self) -> None:
        """Prove exact-repository issues:write access without creating an issue."""

        budget = _RequestBudget(self.limits.request_budget)
        self._validate_installation_binding(budget)
        token = self._installation_access_token(budget)
        response = self._request_with_token(
            "GET",
            self._issues_path(),
            token=token,
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 1,
                "page": 1,
            },
            budget=budget,
        )
        if response.status_code != 200:
            raise GitHubIssueProviderResponseError(
                "GitHub issue validation returned an unexpected status",
                status_code=response.status_code,
            )
        issues = self._as_list(response.payload)
        if len(issues) > 1:
            raise GitHubIssueProviderResponseError(
                "GitHub issue validation exceeded its response limit"
            )

    def publish(
        self,
        title: str,
        body: str,
        delivery_id: UUID,
    ) -> GitHubIssueDeliveryReceipt:
        """Reconcile the opaque marker before creating one repository-bound issue."""

        if not isinstance(delivery_id, UUID):
            raise GitHubIssueConfigurationError("GitHub issue delivery identifier is invalid")
        normalized_delivery_id = str(delivery_id)
        normalized_title = self._validated_text(
            title,
            field="title",
            maximum=self.limits.max_title_length,
            allow_empty=False,
        )
        rendered_body = self._render_body(body, normalized_delivery_id)

        budget = _RequestBudget(self.limits.request_budget)
        self._validate_installation_binding(budget)
        token = self._installation_access_token(budget)
        existing = self._reconcile(
            normalized_delivery_id,
            expected_title=normalized_title,
            expected_body=rendered_body,
            token=token,
            budget=budget,
        )
        if existing is not None:
            return existing

        response = self._request_with_token(
            "POST",
            self._issues_path(),
            token=token,
            json_body={"title": normalized_title, "body": rendered_body},
            budget=budget,
            delivery_write=True,
        )
        if response.status_code != 201:
            raise GitHubIssueAmbiguousDeliveryError(
                "GitHub acknowledged issue creation with an unexpected status",
                status_code=response.status_code,
            )
        try:
            return self._normalize_receipt(
                self._as_object(response.payload),
                delivery_id=normalized_delivery_id,
                reconciled=False,
            )
        except GitHubIssueProviderResponseError:
            raise GitHubIssueAmbiguousDeliveryError(
                "GitHub acknowledged issue creation without a usable receipt",
                status_code=response.status_code,
            ) from None

    def _validate_installation_binding(self, budget: _RequestBudget) -> None:
        response = self._send(
            "GET",
            self._repository_installation_path(),
            headers={
                **self._base_headers(),
                "Authorization": f"Bearer {self.build_app_jwt()}",
            },
            budget=budget,
        )
        if response.status_code != 200:
            raise GitHubIssueProviderResponseError(
                "GitHub repository installation lookup returned an unexpected status",
                status_code=response.status_code,
            )
        installation = self._as_object(response.payload)
        raw_installation_id = installation.get("id")
        if (
            isinstance(raw_installation_id, bool)
            or not isinstance(raw_installation_id, int)
            or raw_installation_id != self.installation_id
        ):
            raise GitHubIssueAuthenticationError(
                "GitHub App installation does not match the configured repository"
            )

    def _installation_access_token(self, budget: _RequestBudget) -> str:
        current_time = self._current_time()
        cached = self._installation_token
        if cached is not None and current_time + timedelta(seconds=60) < cached.expires_at:
            return cached.value

        response = self._send(
            "POST",
            self._installation_token_path(),
            headers={
                **self._base_headers(),
                "Authorization": f"Bearer {self.build_app_jwt()}",
            },
            json_body={
                "repositories": [self.repo],
                "permissions": {"issues": "write"},
            },
            budget=budget,
        )
        if response.status_code != 201:
            raise GitHubIssueProviderResponseError(
                "GitHub installation token exchange returned an unexpected status",
                status_code=response.status_code,
            )
        payload = self._as_object(response.payload)
        token = payload.get("token")
        expires_at = self._parse_timestamp(payload.get("expires_at"))
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8_192
            or _TOKEN_CONTROL_PATTERN.search(token) is not None
            or expires_at is None
            or expires_at <= current_time
            or expires_at > current_time + timedelta(hours=24)
        ):
            raise GitHubIssueProviderResponseError(
                "GitHub installation token response was malformed"
            )

        permission_object = self._as_object(payload.get("permissions"))
        if (
            permission_object.get("issues") != "write"
            or not set(permission_object).issubset({"issues", "metadata"})
            or permission_object.get("metadata", "read") != "read"
        ):
            raise GitHubIssuePermissionError(
                "GitHub App installation did not grant least-privilege issue access"
            )
        repository_items = self._as_list(payload.get("repositories"))
        if len(repository_items) != 1:
            raise GitHubIssueAuthenticationError(
                "GitHub installation token was not restricted to one repository"
            )
        repository_payload = self._as_object(repository_items[0])
        full_name = repository_payload.get("full_name")
        if not isinstance(full_name, str) or full_name.casefold() != self.repository.casefold():
            raise GitHubIssueAuthenticationError(
                "GitHub installation token repository binding was invalid"
            )

        self._installation_token = _InstallationToken(token, expires_at)
        return token

    def _request_with_token(
        self,
        method: str,
        path: str,
        *,
        token: str,
        budget: _RequestBudget,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, JsonValue] | None = None,
        delivery_write: bool = False,
    ) -> _GitHubResponse:
        return self._send(
            method,
            path,
            headers={**self._base_headers(), "Authorization": f"Bearer {token}"},
            params=params,
            json_body=json_body,
            budget=budget,
            delivery_write=delivery_write,
        )

    def _reconcile(
        self,
        delivery_id: str,
        *,
        expected_title: str,
        expected_body: str,
        token: str,
        budget: _RequestBudget,
    ) -> GitHubIssueDeliveryReceipt | None:
        marker = self._delivery_marker(delivery_id)
        matches: list[GitHubIssueDeliveryReceipt] = []
        for page in range(1, self.limits.max_reconciliation_pages + 1):
            response = self._request_with_token(
                "GET",
                self._issues_path(),
                token=token,
                params={
                    "state": "all",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": self.limits.reconciliation_page_size,
                    "page": page,
                },
                budget=budget,
            )
            if response.status_code != 200:
                raise GitHubIssueProviderResponseError(
                    "GitHub issue reconciliation returned an unexpected status",
                    status_code=response.status_code,
                )
            issues = self._as_list(response.payload)
            if len(issues) > self.limits.reconciliation_page_size:
                raise GitHubIssueProviderResponseError(
                    "GitHub issue reconciliation exceeded its page limit"
                )
            for raw_issue in issues:
                issue = self._as_object(raw_issue)
                if "pull_request" in issue:
                    continue
                raw_body = issue.get("body")
                if raw_body is None:
                    continue
                if not isinstance(raw_body, str):
                    raise GitHubIssueProviderResponseError(
                        "GitHub issue reconciliation returned a malformed body"
                    )
                if marker in raw_body:
                    if issue.get("title") != expected_title or raw_body != expected_body:
                        raise GitHubIssueReconciliationAmbiguityError(
                            "GitHub issue delivery content did not match the approved output"
                        )
                    matches.append(
                        self._normalize_receipt(
                            issue,
                            delivery_id=delivery_id,
                            reconciled=True,
                        )
                    )

            has_next = self._has_next_page(response.headers)
            if not has_next:
                break
            if page == self.limits.max_reconciliation_pages:
                raise GitHubIssueReconciliationAmbiguityError(
                    "GitHub issue reconciliation window was incomplete"
                )

        if len(matches) > 1:
            raise GitHubIssueReconciliationAmbiguityError(
                "GitHub issue reconciliation found multiple matching receipts"
            )
        return matches[0] if matches else None

    def _normalize_receipt(
        self,
        issue: Mapping[str, JsonValue],
        *,
        delivery_id: str,
        reconciled: bool,
    ) -> GitHubIssueDeliveryReceipt:
        raw_number = issue.get("number")
        if (
            isinstance(raw_number, bool)
            or not isinstance(raw_number, int)
            or not 1 <= raw_number <= 9_223_372_036_854_775_807
        ):
            raise GitHubIssueProviderResponseError(
                "GitHub issue response contained an invalid issue number"
            )
        canonical_url = f"{GITHUB_HTML_ORIGIN}/{self.repository}/issues/{raw_number}"
        raw_url = issue.get("html_url")
        if not isinstance(raw_url, str) or not self._matches_issue_url(
            raw_url,
            raw_number,
        ):
            raise GitHubIssueProviderResponseError(
                "GitHub issue response contained an invalid issue URL"
            )
        return GitHubIssueDeliveryReceipt(
            delivery_id=delivery_id,
            repository=self.repository,
            issue_number=raw_number,
            issue_url=canonical_url,
            reconciled=reconciled,
        )

    def _render_body(self, body: str, delivery_id: str) -> str:
        normalized_body = self._validated_text(
            body,
            field="body",
            maximum=self.limits.max_body_length,
            allow_empty=True,
        )
        marker = self._delivery_marker(delivery_id)
        marker_occurrences = normalized_body.count("pageragent-delivery:")
        exact_occurrences = normalized_body.count(marker)
        if marker_occurrences:
            if marker_occurrences != 1 or exact_occurrences != 1:
                raise GitHubIssueConfigurationError(
                    "GitHub issue body contained conflicting delivery metadata"
                )
            rendered = normalized_body
        else:
            rendered = f"{normalized_body}\n\n{marker}" if normalized_body else marker
        if len(rendered) > self.limits.max_body_length:
            raise GitHubIssueRequestTooLargeError(
                "GitHub issue body exceeded the configured length limit"
            )
        return rendered

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        budget: _RequestBudget,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, JsonValue] | None = None,
        delivery_write: bool = False,
    ) -> _GitHubResponse:
        self._validate_request_target(method, path)
        request_body = self._encode_request(json_body) if json_body is not None else None
        budget.consume()
        url = f"{GITHUB_API_ORIGIN}{path}"
        try:
            with self._request_lock:
                with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    content=request_body,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    status_code = response.status_code
                    if 300 <= status_code < 400:
                        raise GitHubIssueRedirectError(
                            "GitHub API redirects are not allowed",
                            status_code=status_code,
                        )
                    if self._is_rate_limited(status_code, response.headers):
                        raise GitHubIssueRateLimitError(
                            "GitHub API rate limit prevented the request",
                            status_code=status_code,
                            retry_after_seconds=self._retry_after_seconds(response.headers),
                        )
                    if status_code == 401:
                        raise GitHubIssueAuthenticationError(
                            "GitHub App authentication was rejected",
                            status_code=status_code,
                        )
                    if status_code == 403:
                        raise GitHubIssuePermissionError(
                            "GitHub denied access to the configured repository",
                            status_code=status_code,
                        )
                    if status_code == 404:
                        raise GitHubIssueNotFoundError(
                            "GitHub repository resource was not found",
                            status_code=status_code,
                        )
                    if status_code >= 500 or status_code in {408, 425}:
                        error_type = (
                            GitHubIssueAmbiguousDeliveryError
                            if delivery_write
                            else GitHubIssueRetryableError
                        )
                        raise error_type(
                            "GitHub API request failed transiently",
                            status_code=status_code,
                        )
                    if not 200 <= status_code < 300:
                        raise GitHubIssuePermanentError(
                            "GitHub API rejected the issue request",
                            status_code=status_code,
                        )
                    try:
                        response_body = self._read_bounded_body(response)
                    except (
                        GitHubIssueResponseTooLargeError,
                        GitHubIssueProviderResponseError,
                    ):
                        if delivery_write:
                            raise GitHubIssueAmbiguousDeliveryError(
                                "GitHub issue delivery response was unreadable",
                                status_code=status_code,
                            ) from None
                        raise
                    response_headers = dict(response.headers)
        except GitHubIssuePublisherError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            error_type = (
                GitHubIssueAmbiguousDeliveryError
                if delivery_write
                else GitHubIssueRetryableError
            )
            raise error_type("GitHub API request failed transiently") from None

        if not response_body:
            if delivery_write:
                raise GitHubIssueAmbiguousDeliveryError(
                    "GitHub issue delivery response was empty",
                    status_code=status_code,
                )
            raise GitHubIssueProviderResponseError(
                "GitHub API returned an empty response",
                status_code=status_code,
            )
        try:
            payload = json.loads(
                response_body,
                object_pairs_hook=self._reject_duplicate_keys,
                parse_constant=self._reject_json_constant,
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            _DuplicateKeyError,
            ValueError,
            RecursionError,
        ):
            if delivery_write:
                raise GitHubIssueAmbiguousDeliveryError(
                    "GitHub issue delivery response contained malformed JSON",
                    status_code=status_code,
                ) from None
            raise GitHubIssueProviderResponseError(
                "GitHub API returned malformed JSON",
                status_code=status_code,
            ) from None
        return _GitHubResponse(status_code, payload, response_headers)

    def _validate_request_target(self, method: str, path: str) -> None:
        allowed_targets = {
            ("GET", self._repository_installation_path()),
            ("POST", self._installation_token_path()),
            ("GET", self._issues_path()),
            ("POST", self._issues_path()),
        }
        if (method, path) not in allowed_targets:
            raise GitHubIssueConfigurationError("GitHub issue API target is not allowed")

    def _encode_request(self, payload: Mapping[str, JsonValue]) -> bytes:
        try:
            request_body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise GitHubIssueConfigurationError(
                "GitHub issue request payload is invalid"
            ) from None
        if len(request_body) > self.limits.max_request_bytes:
            raise GitHubIssueRequestTooLargeError(
                "GitHub issue request exceeded the configured byte limit"
            )
        return request_body

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise GitHubIssueProviderResponseError(
                    "GitHub API returned an invalid response length"
                )
            if int(content_length) > self.limits.max_response_bytes:
                raise GitHubIssueResponseTooLargeError(
                    "GitHub API response exceeded the configured byte limit"
                )
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > self.limits.max_response_bytes:
                raise GitHubIssueResponseTooLargeError(
                    "GitHub API response exceeded the configured byte limit"
                )
            body.extend(chunk)
        return bytes(body)

    def _matches_issue_url(self, raw_url: str, issue_number: int) -> bool:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except (TypeError, ValueError):
            return False
        expected_path = f"/{self.repository}/issues/{issue_number}"
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and parsed.path.casefold() == expected_path.casefold()
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _has_next_page(headers: Mapping[str, str]) -> bool:
        link = headers.get("link")
        if link is None:
            return False
        if (
            not isinstance(link, str)
            or len(link) > 4_096
            or _LINK_CONTROL_PATTERN.search(link) is not None
        ):
            raise GitHubIssueProviderResponseError(
                "GitHub pagination response was malformed"
            )
        for entry in link.split(","):
            if re.search(r";\s*rel=\"[^\"]*\bnext\b[^\"]*\"", entry):
                return True
        return False

    @staticmethod
    def _is_rate_limited(status_code: int, headers: httpx.Headers) -> bool:
        return status_code == 429 or (
            status_code == 403
            and (
                headers.get("retry-after") is not None
                or headers.get("x-ratelimit-remaining") == "0"
            )
        )

    @staticmethod
    def _retry_after_seconds(headers: httpx.Headers) -> int | None:
        raw_value = headers.get("retry-after")
        if raw_value is None or not raw_value.isascii() or not raw_value.isdigit():
            return None
        retry_after = int(raw_value)
        return retry_after if 0 <= retry_after <= 86_400 else None

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "PagerAgent-GitHub-Issues/1",
            "Content-Type": "application/json",
        }

    def _repository_installation_path(self) -> str:
        return self._path("repos", self.owner, self.repo, "installation")

    def _installation_token_path(self) -> str:
        return self._path(
            "app",
            "installations",
            str(self.installation_id),
            "access_tokens",
        )

    def _issues_path(self) -> str:
        return self._path("repos", self.owner, self.repo, "issues")

    @staticmethod
    def _path(*segments: str) -> str:
        return "/" + "/".join(quote(segment, safe="") for segment in segments)

    @staticmethod
    def _delivery_marker(delivery_id: str) -> str:
        return f"<!-- pageragent-delivery:{delivery_id} -->"

    @staticmethod
    def _validated_text(
        value: str,
        *,
        field: str,
        maximum: int,
        allow_empty: bool,
    ) -> str:
        if (
            not isinstance(value, str)
            or len(value) > maximum
            or (not allow_empty and not value.strip())
            or _TEXT_CONTROL_PATTERN.search(value) is not None
        ):
            raise GitHubIssueConfigurationError(f"GitHub issue {field} is invalid")
        return value

    @staticmethod
    def _as_object(value: JsonValue) -> dict[str, JsonValue]:
        if not isinstance(value, dict):
            raise GitHubIssueProviderResponseError(
                "GitHub API returned a malformed object"
            )
        return value

    @staticmethod
    def _as_list(value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            raise GitHubIssueProviderResponseError(
                "GitHub API returned a malformed list"
            )
        return value

    @staticmethod
    def _parse_timestamp(value: JsonValue) -> datetime | None:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    def _current_time(self) -> datetime:
        current_time = self._now()
        if (
            not isinstance(current_time, datetime)
            or current_time.tzinfo is None
            or current_time.utcoffset() is None
        ):
            raise GitHubIssueConfigurationError("GitHub issue publisher clock is invalid")
        return current_time.astimezone(UTC)

    @staticmethod
    def _reject_duplicate_keys(
        pairs: list[tuple[str, JsonValue]],
    ) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKeyError
            result[key] = value
        return result

    @staticmethod
    def _reject_json_constant(_constant: str) -> JsonValue:
        raise ValueError
