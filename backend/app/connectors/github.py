import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from urllib.parse import quote

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.domain.connectors import GithubConfiguration, GithubCredentials
from app.domain.github import (
    CommitRecord,
    GitCommitFile,
    GitCommitStats,
    GitDeployment,
    GitDeploymentStatus,
    GitEvidenceBundle,
    GitPullRequest,
    GitRelease,
)
from app.investigation.commits import sha_matches

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Clock = Callable[[], datetime]

GITHUB_API_ORIGIN = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2026-03-10"
_OWNER_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REPOSITORY_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_API_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")


class GitHubProviderError(RuntimeError):
    """Sanitized provider failure that never includes credentials or response bodies."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubConfigurationError(GitHubProviderError):
    pass


class GitHubAuthenticationError(GitHubProviderError):
    pass


class GitHubRateLimitError(GitHubProviderError):
    retryable = True

    def __init__(
        self,
        *,
        status_code: int,
        retry_after_seconds: int | None,
        reset_at: datetime | None,
    ) -> None:
        super().__init__(
            "GitHub rate limit prevented evidence collection",
            status_code=status_code,
        )
        self.retry_after_seconds = retry_after_seconds
        self.reset_at = reset_at


class GitHubRetryableError(GitHubProviderError):
    retryable = True


class GitHubRedirectError(GitHubProviderError):
    pass


class GitHubResponseTooLargeError(GitHubProviderError):
    pass


class GitHubRequestBudgetExceededError(GitHubProviderError):
    pass


class GitHubProviderResponseError(GitHubProviderError):
    pass


class GitHubNotFoundError(GitHubProviderError):
    pass


@dataclass(frozen=True)
class GitHubClientLimits:
    request_budget: int = 24
    max_response_bytes: int = 512 * 1024
    timeout_seconds: float = 5.0
    lookback_hours: int = 2
    max_commits: int = 8
    max_files_per_commit: int = 50
    max_pull_requests: int = 10
    max_deployments: int = 6
    max_releases: int = 6

    def __post_init__(self) -> None:
        bounded_values = (
            self.request_budget,
            self.max_response_bytes,
            self.lookback_hours,
            self.max_commits,
            self.max_files_per_commit,
            self.max_pull_requests,
            self.max_deployments,
            self.max_releases,
        )
        if any(value <= 0 for value in bounded_values):
            raise ValueError("GitHub client limits must be positive")
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ValueError("GitHub HTTP timeout must be between 0.1 and 30 seconds")
        if self.request_budget > 100 or self.max_response_bytes > 4 * 1024 * 1024:
            raise ValueError("GitHub client limits exceed the supported safety boundary")
        if self.lookback_hours > 168:
            raise ValueError("GitHub evidence lookback cannot exceed 168 hours")
        if self.max_commits > 100 or self.max_files_per_commit > 100:
            raise ValueError("GitHub commit limits cannot exceed 100 items")
        if any(
            value > 50
            for value in (
                self.max_pull_requests,
                self.max_deployments,
                self.max_releases,
            )
        ):
            raise ValueError("GitHub related-evidence limits cannot exceed 50 items")


@dataclass
class _RequestBudget:
    remaining: int

    def consume(self) -> None:
        if self.remaining <= 0:
            raise GitHubRequestBudgetExceededError(
                "GitHub evidence request budget was exhausted"
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


def _system_clock() -> datetime:
    return datetime.now(UTC)


class GitHubAppEvidenceProvider:
    """A bounded, synchronous GitHub App adapter for normalized incident evidence."""

    version = "github-app-v1"

    def __init__(
        self,
        configuration: GithubConfiguration,
        credentials: GithubCredentials,
        *,
        api_version: str = DEFAULT_GITHUB_API_VERSION,
        client: httpx.Client | None = None,
        now: Clock = _system_clock,
        limits: GitHubClientLimits = GitHubClientLimits(),
    ) -> None:
        configured_origin = (configuration.api_url or GITHUB_API_ORIGIN).rstrip("/")
        if configured_origin != GITHUB_API_ORIGIN:
            raise GitHubConfigurationError(
                "GitHub App evidence supports only the public GitHub API origin"
            )
        if _API_VERSION_PATTERN.fullmatch(api_version) is None:
            raise GitHubConfigurationError("GitHub API version is invalid")

        repository_parts = configuration.repository.split("/")
        if len(repository_parts) != 2:
            raise GitHubConfigurationError("GitHub repository configuration is invalid")
        self.owner = self._validated_segment(
            repository_parts[0],
            "repository owner",
            pattern=_OWNER_SEGMENT_PATTERN,
        )
        self.repo = self._validated_segment(
            repository_parts[1],
            "repository name",
            pattern=_REPOSITORY_SEGMENT_PATTERN,
        )
        self.repository = f"{self.owner}/{self.repo}"
        self.app_id = configuration.app_id
        self.installation_id = configuration.installation_id
        self.api_version = api_version
        self.limits = limits
        self._now = now
        self._request_lock = RLock()
        self._installation_token: _InstallationToken | None = None

        try:
            private_key = serialization.load_pem_private_key(
                credentials.private_key.get_secret_value().encode("utf-8"),
                password=None,
            )
        except (TypeError, ValueError, UnsupportedAlgorithm):
            raise GitHubConfigurationError("GitHub App private key is invalid") from None
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise GitHubConfigurationError("GitHub App private key must be an RSA key")
        if private_key.key_size < 2_048:
            raise GitHubConfigurationError("GitHub App private key must be at least 2048 bits")
        self._private_key = private_key

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

    def __enter__(self) -> "GitHubAppEvidenceProvider":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def build_app_jwt(self) -> str:
        """Build a short-lived App JWT; exposed for connector lifecycle validation."""

        current = self._current_time()
        now_timestamp = int(current.timestamp())
        claims = {
            "iat": now_timestamp - 60,
            "exp": now_timestamp + (9 * 60),
            "iss": str(self.app_id),
        }
        try:
            return jwt.encode(claims, self._private_key, algorithm="RS256")
        except (TypeError, ValueError, jwt.PyJWTError):
            raise GitHubAuthenticationError("GitHub App JWT signing failed") from None

    def validate(self) -> None:
        """Validate App installation auth and repository-scoped access."""

        budget = _RequestBudget(self.limits.request_budget)
        self._validate_installation_binding(budget)
        self._request_api(
            "GET",
            self._path("repos", self.owner, self.repo),
            budget=budget,
        )

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        deployed_at = self._aware_utc(deployed_at)
        service = self._bounded_text(service, 200, fallback="unknown-service")
        active_commit_sha = self._validated_sha(active_commit_sha)
        budget = _RequestBudget(self.limits.request_budget)
        self._validate_installation_binding(budget)

        commits = self._collect_commits(
            deployed_at,
            service,
            active_commit_sha,
            budget,
        )
        pull_requests = self._collect_pull_requests(budget)
        deployments = self._collect_deployments(budget)
        releases = self._collect_releases(budget)
        return GitEvidenceBundle(
            source_uri=f"github://{self.repository}",
            provider="github_app",
            repository=self.repository,
            provider_version=self.version,
            deployed_at=deployed_at,
            service=service,
            active_commit_sha=active_commit_sha,
            commits=commits,
            pull_requests=pull_requests,
            deployments=deployments,
            releases=releases,
        )

    def _validate_installation_binding(self, budget: _RequestBudget) -> None:
        """Prove the configured installation belongs to this exact repository."""

        response = self._send(
            "GET",
            self._path("repos", self.owner, self.repo, "installation"),
            headers={
                **self._base_headers(),
                "Authorization": f"Bearer {self.build_app_jwt()}",
            },
            budget=budget,
        )
        installation = self._as_object(response.payload)
        if self._positive_int(installation.get("id")) != self.installation_id:
            raise GitHubAuthenticationError(
                "GitHub App installation does not match the configured repository"
            )

    def _collect_commits(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
        budget: _RequestBudget,
    ) -> list[CommitRecord]:
        payload = self._request_api(
            "GET",
            self._path("repos", self.owner, self.repo, "commits"),
            params={
                "since": self._format_timestamp(
                    deployed_at - timedelta(hours=self.limits.lookback_hours)
                ),
                "until": self._format_timestamp(deployed_at + timedelta(minutes=15)),
                "per_page": self.limits.max_commits,
                "page": 1,
            },
            budget=budget,
        )
        summaries = self._as_list(payload)
        commits: list[CommitRecord] = []
        for summary in summaries[: self.limits.max_commits]:
            summary_object = self._as_object(summary)
            sha = self._validated_sha(summary_object.get("sha"))
            detail = self._request_api(
                "GET",
                self._path("repos", self.owner, self.repo, "commits", sha),
                params={"per_page": self.limits.max_files_per_commit, "page": 1},
                budget=budget,
            )
            normalized = self._normalize_commit(self._as_object(detail), service)
            if not any(sha_matches(normalized.sha, item.sha) for item in commits):
                commits.append(normalized)

        if not any(sha_matches(active_commit_sha, commit.sha) for commit in commits):
            try:
                active_payload = self._request_api(
                    "GET",
                    self._path(
                        "repos",
                        self.owner,
                        self.repo,
                        "commits",
                        active_commit_sha,
                    ),
                    params={"per_page": self.limits.max_files_per_commit, "page": 1},
                    budget=budget,
                )
            except GitHubNotFoundError:
                # Alert SHAs can refer to another repository in local/demo data.
                # A missing optional lookup must not discard the bounded catalog.
                pass
            else:
                active_commit = self._normalize_commit(
                    self._as_object(active_payload),
                    service,
                )
                if not any(sha_matches(active_commit.sha, item.sha) for item in commits):
                    commits = sorted(
                        commits,
                        key=lambda commit: (commit.committed_at, commit.sha.lower()),
                        reverse=True,
                    )[: self.limits.max_commits - 1]
                    commits.append(active_commit)

        return sorted(
            commits,
            key=lambda commit: (commit.committed_at, commit.sha.lower()),
            reverse=True,
        )[: self.limits.max_commits]

    def _collect_pull_requests(self, budget: _RequestBudget) -> list[GitPullRequest]:
        payload = self._request_api(
            "GET",
            self._path("repos", self.owner, self.repo, "pulls"),
            params={
                "state": "all",
                "sort": "updated",
                "direction": "desc",
                "per_page": self.limits.max_pull_requests,
                "page": 1,
            },
            budget=budget,
        )
        normalized = [
            self._normalize_pull_request(self._as_object(item))
            for item in self._as_list(payload)[: self.limits.max_pull_requests]
        ]
        return sorted(
            normalized,
            key=lambda item: (item.updated_at, item.number),
            reverse=True,
        )

    def _collect_deployments(self, budget: _RequestBudget) -> list[GitDeployment]:
        payload = self._request_api(
            "GET",
            self._path("repos", self.owner, self.repo, "deployments"),
            params={"per_page": self.limits.max_deployments, "page": 1},
            budget=budget,
        )
        deployments: list[GitDeployment] = []
        for item in self._as_list(payload)[: self.limits.max_deployments]:
            deployment = self._as_object(item)
            deployment_id = self._positive_int(deployment.get("id"))
            status_payload = self._request_api(
                "GET",
                self._path(
                    "repos",
                    self.owner,
                    self.repo,
                    "deployments",
                    str(deployment_id),
                    "statuses",
                ),
                params={"per_page": 1, "page": 1},
                budget=budget,
            )
            statuses = self._as_list(status_payload)
            latest_status = (
                self._normalize_deployment_status(self._as_object(statuses[0]))
                if statuses
                else None
            )
            deployments.append(
                self._normalize_deployment(deployment, latest_status=latest_status)
            )
        return sorted(
            deployments,
            key=lambda item: (item.created_at, item.deployment_id),
            reverse=True,
        )

    def _collect_releases(self, budget: _RequestBudget) -> list[GitRelease]:
        payload = self._request_api(
            "GET",
            self._path("repos", self.owner, self.repo, "releases"),
            params={"per_page": self.limits.max_releases, "page": 1},
            budget=budget,
        )
        normalized = [
            self._normalize_release(self._as_object(item))
            for item in self._as_list(payload)[: self.limits.max_releases]
        ]
        return sorted(
            normalized,
            key=lambda item: (item.published_at or item.created_at, item.release_id),
            reverse=True,
        )

    def _request_api(
        self,
        method: str,
        path: str,
        *,
        budget: _RequestBudget,
        params: Mapping[str, str | int] | None = None,
    ) -> JsonValue:
        token = self._installation_access_token(budget)
        response = self._send(
            method,
            path,
            headers=self._installation_headers(token),
            params=params,
            budget=budget,
            allow_unauthorized=True,
        )
        if response.status_code == 401:
            self._installation_token = None
            refreshed_token = self._installation_access_token(budget)
            response = self._send(
                method,
                path,
                headers=self._installation_headers(refreshed_token),
                params=params,
                budget=budget,
                allow_unauthorized=True,
            )
            if response.status_code == 401:
                self._installation_token = None
                raise GitHubAuthenticationError(
                    "GitHub installation authentication was rejected",
                    status_code=401,
                )
        return response.payload

    def _installation_access_token(self, budget: _RequestBudget) -> str:
        current = self._current_time()
        cached = self._installation_token
        if cached is not None and current + timedelta(seconds=60) < cached.expires_at:
            return cached.value

        path = self._path(
            "app",
            "installations",
            str(self.installation_id),
            "access_tokens",
        )
        response = self._send(
            "POST",
            path,
            headers={
                **self._base_headers(),
                "Authorization": f"Bearer {self.build_app_jwt()}",
            },
            json_body={
                "repositories": [self.repo],
                "permissions": {
                    "contents": "read",
                    "pull_requests": "read",
                    "deployments": "read",
                },
            },
            budget=budget,
        )
        token_payload = self._as_object(response.payload)
        token = token_payload.get("token")
        expires_at = self._parse_timestamp(token_payload.get("expires_at"))
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8_192
            or _CONTROL_CHARACTER_PATTERN.search(token) is not None
            or expires_at is None
        ):
            raise GitHubProviderResponseError(
                "GitHub installation token response was malformed"
            )
        if expires_at <= current:
            raise GitHubAuthenticationError("GitHub installation token was already expired")
        self._installation_token = _InstallationToken(token, expires_at)
        return token

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        budget: _RequestBudget,
        params: Mapping[str, str | int] | None = None,
        json_body: Mapping[str, JsonValue] | None = None,
        allow_unauthorized: bool = False,
    ) -> _GitHubResponse:
        if method not in {"GET", "POST"}:
            raise GitHubConfigurationError("Unsupported GitHub request method")
        self._validate_relative_path(path)
        budget.consume()
        url = f"{GITHUB_API_ORIGIN}{path}"
        try:
            with self._request_lock:
                with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise GitHubRedirectError(
                            "GitHub API redirects are not allowed",
                            status_code=response.status_code,
                        )
                    response_body = self._read_bounded_body(response)
                    response_headers = dict(response.headers)
                    status_code = response.status_code
        except GitHubProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise GitHubRetryableError("GitHub API request failed transiently") from None

        if status_code == 401 and allow_unauthorized:
            return _GitHubResponse(status_code=status_code, payload=None)
        if not 200 <= status_code < 300:
            self._raise_for_status(status_code, response_headers)
        if not response_body:
            payload: JsonValue = None
        else:
            try:
                payload = json.loads(response_body)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
                raise GitHubProviderResponseError(
                    "GitHub API returned malformed JSON"
                ) from None
        return _GitHubResponse(status_code=status_code, payload=payload)

    def _read_bounded_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            if not content_length.isascii() or not content_length.isdigit():
                raise GitHubProviderResponseError(
                    "GitHub API returned an invalid response length"
                )
            if int(content_length) > self.limits.max_response_bytes:
                raise GitHubResponseTooLargeError(
                    "GitHub API response exceeded the configured byte limit"
                )

        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > self.limits.max_response_bytes:
                raise GitHubResponseTooLargeError(
                    "GitHub API response exceeded the configured byte limit"
                )
            body.extend(chunk)
        return bytes(body)

    def _raise_for_status(self, status_code: int, headers: Mapping[str, str]) -> None:
        rate_limited = (
            status_code == 429
            or headers.get("retry-after") is not None
            or headers.get("x-ratelimit-remaining") == "0"
        )
        if status_code in {403, 429} and rate_limited:
            raise GitHubRateLimitError(
                status_code=status_code,
                retry_after_seconds=self._bounded_header_int(
                    headers.get("retry-after"),
                    maximum=86_400,
                ),
                reset_at=self._rate_limit_reset(headers.get("x-ratelimit-reset")),
            )
        if status_code >= 500:
            raise GitHubRetryableError(
                "GitHub API is temporarily unavailable",
                status_code=status_code,
            )
        if status_code == 401:
            raise GitHubAuthenticationError(
                "GitHub authentication was rejected",
                status_code=status_code,
            )
        if status_code == 403:
            raise GitHubProviderError(
                "GitHub denied access to the configured repository evidence",
                status_code=status_code,
            )
        if status_code == 404:
            raise GitHubNotFoundError(
                "GitHub resource was not found",
                status_code=status_code,
            )
        raise GitHubProviderError(
            "GitHub API rejected the evidence request",
            status_code=status_code,
        )

    def _normalize_commit(
        self,
        payload: dict[str, JsonValue],
        service: str,
    ) -> CommitRecord:
        sha = self._validated_sha(payload.get("sha"))
        commit = self._as_object(payload.get("commit"))
        message = self._bounded_title(commit.get("message"), fallback="Untitled commit")
        provider_author = self._optional_object(payload.get("author"))
        embedded_author = self._optional_object(commit.get("author"))
        author = self._bounded_text(
            (provider_author or {}).get("login")
            or (embedded_author or {}).get("name"),
            200,
            fallback="unknown",
        )
        embedded_committer = self._optional_object(commit.get("committer"))
        committed_at = self._parse_timestamp(
            (embedded_committer or {}).get("date")
            or (embedded_author or {}).get("date")
        )
        if committed_at is None:
            raise GitHubProviderResponseError("GitHub commit timestamp was malformed")

        raw_files = self._as_list(payload.get("files", []))
        file_objects = [self._as_object(item) for item in raw_files]
        file_objects.sort(key=lambda item: self._bounded_text(item.get("filename"), 500))
        files = [
            self._normalize_commit_file(item)
            for item in file_objects[: self.limits.max_files_per_commit]
        ]
        stats_payload = self._optional_object(payload.get("stats")) or {}
        additions = self._non_negative_int(stats_payload.get("additions"))
        deletions = self._non_negative_int(stats_payload.get("deletions"))
        total = self._non_negative_int(stats_payload.get("total"))
        stats = GitCommitStats(
            additions=additions,
            deletions=deletions,
            total=total,
        )
        change_types = self._derive_change_types([file.path for file in files])
        file_names = [file.path for file in files]
        listed_names = ", ".join(file_names[:5]) or "no file names returned"
        diff_summary = self._bounded_text(
            f"{len(files)} files changed (+{additions}/-{deletions}): {listed_names}",
            1_000,
        )
        return CommitRecord(
            sha=sha,
            title=message,
            author=author,
            committed_at=committed_at,
            services=[service],
            owners=[author],
            change_types=change_types,
            files_changed=file_names,
            diff_summary=diff_summary,
            stats=stats,
            files=files,
        )

    def _normalize_commit_file(self, payload: dict[str, JsonValue]) -> GitCommitFile:
        path = self._normalized_file_path(payload.get("filename"))
        status = self._bounded_text(payload.get("status"), 20, fallback="changed").lower()
        if status not in {"added", "modified", "removed", "renamed", "copied", "changed"}:
            status = "changed"
        additions = self._non_negative_int(payload.get("additions"))
        deletions = self._non_negative_int(payload.get("deletions"))
        changes = self._non_negative_int(payload.get("changes"))
        return GitCommitFile(
            path=path,
            status=status,
            additions=additions,
            deletions=deletions,
            changes=changes,
        )

    def _normalize_pull_request(self, payload: dict[str, JsonValue]) -> GitPullRequest:
        state_value = self._bounded_text(payload.get("state"), 20, fallback="closed").lower()
        state = "open" if state_value == "open" else "closed"
        merged_at = self._parse_timestamp(payload.get("merged_at"))
        user = self._optional_object(payload.get("user")) or {}
        head = self._optional_object(payload.get("head")) or {}
        base = self._optional_object(payload.get("base")) or {}
        labels = sorted(
            {
                self._bounded_text(
                    (self._optional_object(item) or {}).get("name"),
                    100,
                )
                for item in self._as_list(payload.get("labels", []))[:20]
                if self._bounded_text(
                    (self._optional_object(item) or {}).get("name"),
                    100,
                )
            }
        )
        return GitPullRequest(
            number=self._positive_int(payload.get("number")),
            title=self._bounded_title(payload.get("title"), fallback="Untitled pull request"),
            state=state,
            merged=merged_at is not None,
            author=self._bounded_text(user.get("login"), 200, fallback="unknown"),
            head_sha=self._optional_sha(head.get("sha")),
            merge_commit_sha=self._optional_sha(payload.get("merge_commit_sha")),
            base_ref=self._bounded_text(base.get("ref"), 200),
            labels=labels,
            changed_files=self._non_negative_int(payload.get("changed_files"), maximum=1_000_000),
            additions=self._non_negative_int(payload.get("additions")),
            deletions=self._non_negative_int(payload.get("deletions")),
            created_at=self._required_timestamp(payload.get("created_at"), "pull request"),
            updated_at=self._required_timestamp(payload.get("updated_at"), "pull request"),
            merged_at=merged_at,
        )

    def _normalize_deployment(
        self,
        payload: dict[str, JsonValue],
        *,
        latest_status: GitDeploymentStatus | None,
    ) -> GitDeployment:
        return GitDeployment(
            deployment_id=self._positive_int(payload.get("id")),
            sha=self._validated_sha(payload.get("sha")),
            ref=self._bounded_text(payload.get("ref"), 200),
            task=self._bounded_text(payload.get("task"), 100),
            environment=self._bounded_text(payload.get("environment"), 200),
            transient_environment=payload.get("transient_environment") is True,
            production_environment=payload.get("production_environment") is True,
            created_at=self._required_timestamp(payload.get("created_at"), "deployment"),
            updated_at=self._required_timestamp(payload.get("updated_at"), "deployment"),
            latest_status=latest_status,
        )

    def _normalize_deployment_status(
        self,
        payload: dict[str, JsonValue],
    ) -> GitDeploymentStatus:
        state = self._bounded_text(payload.get("state"), 30, fallback="unknown").lower()
        allowed_states = {
            "error",
            "failure",
            "inactive",
            "in_progress",
            "pending",
            "queued",
            "success",
        }
        if state not in allowed_states:
            state = "unknown"
        created_at = self._required_timestamp(payload.get("created_at"), "deployment status")
        updated_at = self._parse_timestamp(payload.get("updated_at")) or created_at
        return GitDeploymentStatus(
            state=state,
            environment=self._bounded_text(payload.get("environment"), 200),
            created_at=created_at,
            updated_at=updated_at,
        )

    def _normalize_release(self, payload: dict[str, JsonValue]) -> GitRelease:
        author = self._optional_object(payload.get("author")) or {}
        return GitRelease(
            release_id=self._positive_int(payload.get("id")),
            tag_name=self._bounded_text(payload.get("tag_name"), 200, fallback="untagged"),
            name=self._bounded_title(payload.get("name"), fallback=""),
            target_commitish=self._bounded_text(payload.get("target_commitish"), 200),
            author=self._bounded_text(author.get("login"), 200, fallback="unknown"),
            draft=payload.get("draft") is True,
            prerelease=payload.get("prerelease") is True,
            created_at=self._required_timestamp(payload.get("created_at"), "release"),
            published_at=self._parse_timestamp(payload.get("published_at")),
        )

    @staticmethod
    def _derive_change_types(paths: list[str]) -> list[str]:
        matched: set[str] = set()
        for raw_path in paths:
            path = raw_path.lower()
            filename = path.rsplit("/", 1)[-1]
            if "/test" in f"/{path}" or filename.startswith("test_") or ".test." in filename:
                matched.add("testing")
            if filename in {
                "package-lock.json",
                "package.json",
                "poetry.lock",
                "pyproject.toml",
                "requirements.txt",
                "go.mod",
                "go.sum",
            }:
                matched.add("dependency_update")
            if any(token in path for token in ("validation", "validator", "schema")):
                matched.add("validation_logic")
            if any(token in path for token in ("migration", "alembic", "/db/", "database")):
                matched.add("database_migration")
            if any(token in path for token in ("telemetry", "observability", "metrics", "tracing")):
                matched.add("observability")
            if any(token in path for token in ("auth", "security", "permission", "policy")):
                matched.add("security")
            if filename.startswith("dockerfile") or any(
                token in path for token in ("terraform", "kubernetes", "helm", ".github/workflows")
            ):
                matched.add("infrastructure")
            if filename.endswith((".yaml", ".yml", ".toml", ".ini", ".conf", ".env")):
                matched.add("configuration")
            if filename.endswith((".md", ".rst")) or path.startswith("docs/"):
                matched.add("documentation")
        if not matched:
            matched.add("application_code")
        precedence = (
            "validation_logic",
            "database_migration",
            "dependency_update",
            "configuration",
            "security",
            "infrastructure",
            "observability",
            "testing",
            "documentation",
            "application_code",
        )
        return [change_type for change_type in precedence if change_type in matched]

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "PagerAgent-GitHub-Evidence/1",
        }

    def _installation_headers(self, token: str) -> dict[str, str]:
        return {**self._base_headers(), "Authorization": f"Bearer {token}"}

    @classmethod
    def _path(cls, *segments: str) -> str:
        return "/" + "/".join(
            quote(cls._validated_segment(segment, "request path segment"), safe="")
            for segment in segments
        )

    @staticmethod
    def _validated_segment(
        value: str,
        field: str,
        *,
        pattern: re.Pattern[str] = _REPOSITORY_SEGMENT_PATTERN,
    ) -> str:
        if value in {".", ".."} or pattern.fullmatch(value) is None:
            raise GitHubConfigurationError(f"GitHub {field} is invalid")
        return value

    @staticmethod
    def _validate_relative_path(path: str) -> None:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or "\\" in path
            or any(segment in {"", ".", ".."} for segment in path.split("/")[1:])
        ):
            raise GitHubConfigurationError("GitHub request path is invalid")

    @staticmethod
    def _validated_sha(value: JsonValue) -> str:
        if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
            raise GitHubProviderResponseError("GitHub commit SHA was malformed")
        return value.lower()

    @classmethod
    def _optional_sha(cls, value: JsonValue) -> str | None:
        if value is None:
            return None
        return cls._validated_sha(value)

    @staticmethod
    def _as_object(value: JsonValue) -> dict[str, JsonValue]:
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise GitHubProviderResponseError("GitHub API response shape was malformed")
        return value

    @staticmethod
    def _optional_object(value: JsonValue) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        return GitHubAppEvidenceProvider._as_object(value)

    @staticmethod
    def _as_list(value: JsonValue) -> list[JsonValue]:
        if not isinstance(value, list):
            raise GitHubProviderResponseError("GitHub API response shape was malformed")
        return value

    @staticmethod
    def _bounded_text(
        value: JsonValue,
        maximum: int,
        *,
        fallback: str = "",
    ) -> str:
        if not isinstance(value, str):
            value = fallback
        sanitized = _CONTROL_CHARACTER_PATTERN.sub(" ", value)
        sanitized = " ".join(sanitized.split())
        if not sanitized and fallback:
            sanitized = _CONTROL_CHARACTER_PATTERN.sub(" ", fallback)
            sanitized = " ".join(sanitized.split())
        return sanitized[:maximum]

    @classmethod
    def _bounded_title(
        cls,
        value: JsonValue,
        *,
        fallback: str,
    ) -> str:
        if isinstance(value, str):
            value = value.splitlines()[0] if value.splitlines() else fallback
        return cls._bounded_text(value, 300, fallback=fallback)

    @classmethod
    def _normalized_file_path(cls, value: JsonValue) -> str:
        path = cls._bounded_text(value, 500)
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(segment in {"", ".", ".."} for segment in path.split("/"))
        ):
            raise GitHubProviderResponseError("GitHub commit file path was malformed")
        return path

    @staticmethod
    def _non_negative_int(value: JsonValue, *, maximum: int = 1_000_000_000) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, min(value, maximum))

    @staticmethod
    def _positive_int(value: JsonValue) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GitHubProviderResponseError("GitHub API identifier was malformed")
        return value

    @staticmethod
    def _parse_timestamp(value: JsonValue) -> datetime | None:
        if not isinstance(value, str) or len(value) > 50:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @classmethod
    def _required_timestamp(cls, value: JsonValue, kind: str) -> datetime:
        parsed = cls._parse_timestamp(value)
        if parsed is None:
            raise GitHubProviderResponseError(f"GitHub {kind} timestamp was malformed")
        return parsed

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise GitHubConfigurationError("GitHub evidence timestamps must include a timezone")
        return value.astimezone(UTC)

    def _current_time(self) -> datetime:
        return self._aware_utc(self._now())

    @staticmethod
    def _format_timestamp(value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _bounded_header_int(value: str | None, *, maximum: int) -> int | None:
        if value is None or len(value) > 20 or not value.isascii() or not value.isdigit():
            return None
        return min(int(value), maximum)

    @classmethod
    def _rate_limit_reset(cls, value: str | None) -> datetime | None:
        timestamp = cls._bounded_header_int(value, maximum=4_102_444_800)
        if timestamp is None:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
