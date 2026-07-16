from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import SecretStr

from app.connectors.github import GitHubAppEvidenceProvider, GitHubClientLimits
from app.domain.connectors import GithubConfiguration, GithubCredentials
from app.domain.github import CommitRecord, GitEvidenceBundle, GitWebhookReceipt
from app.investigation.commits import CommitRanker, FixtureGitProvider, sha_matches
from tests.test_github_app import PRIVATE_KEY_PEM

NOW = datetime(2026, 7, 16, 16, 30, tzinfo=UTC)
RECENT_SHA = "a" * 40
SECOND_SHA = "b" * 40
ACTIVE_SHA = "c" * 40


def provider_for(
    handler,
    *,
    limits: GitHubClientLimits | None = None,
) -> tuple[GitHubAppEvidenceProvider, httpx.Client]:
    downstream = httpx.MockTransport(handler)

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo-org/pageragent/installation":
            return httpx.Response(200, json={"id": 67890}, request=request)
        return downstream.handle_request(request)

    client = httpx.Client(transport=httpx.MockTransport(wrapped_handler))
    provider = GitHubAppEvidenceProvider(
        GithubConfiguration(
            service="checkout-api",
            repository="octo-org/pageragent",
            app_id=12345,
            installation_id=67890,
            api_url="https://api.github.com",
        ),
        GithubCredentials(
            private_key=SecretStr(PRIVATE_KEY_PEM),
            webhook_secret=SecretStr("w" * 32),
        ),
        client=client,
        now=lambda: NOW,
        limits=limits or GitHubClientLimits(),
    )
    return provider, client


def installation_token(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        201,
        json={"token": "memory-only", "expires_at": "2026-07-16T17:30:00Z"},
        request=request,
    )


def commit_payload(sha: str, *, message: str = "Refactor validation") -> dict[str, object]:
    return {
        "sha": sha,
        "html_url": "https://evil.example/SENTINEL_ARBITRARY_URL",
        "commit": {
            "message": message,
            "author": {"name": "Embedded Author", "date": "2026-07-16T16:10:00Z"},
            "committer": {"date": "2026-07-16T16:11:00Z"},
        },
        "author": {"login": "octocat"},
        "stats": {"additions": 12, "deletions": 5, "total": 17},
        "files": [
            {
                "filename": "services/checkout/validation/payment.py",
                "status": "modified",
                "additions": 10,
                "deletions": 5,
                "changes": 15,
                "patch": "SENTINEL_RAW_PATCH",
                "blob_url": "https://evil.example/blob",
            },
            {
                "filename": "services/checkout/tests/test_validator.py",
                "status": "added",
                "additions": 2,
                "deletions": 0,
                "changes": 2,
                "patch": "SENTINEL_TEST_PATCH",
            },
        ],
    }


def test_collects_bounded_normalized_repository_evidence_without_raw_bodies_or_urls() -> None:
    requested: list[httpx.Request] = []
    long_title = "T" * 400 + "\nSENTINEL_COMMIT_BODY"

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token(request)
        if path == "/repos/octo-org/pageragent/commits":
            return httpx.Response(200, json=[{"sha": RECENT_SHA}], request=request)
        if path == f"/repos/octo-org/pageragent/commits/{RECENT_SHA}":
            return httpx.Response(
                200,
                json=commit_payload(RECENT_SHA, message=long_title),
                request=request,
            )
        if path == "/repos/octo-org/pageragent/pulls":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 42,
                        "title": "Fix checkout\nSENTINEL_PR_BODY_IN_TITLE",
                        "body": "SENTINEL_RAW_PR_BODY",
                        "html_url": "https://evil.example/pr",
                        "state": "closed",
                        "merged_at": "2026-07-16T16:15:00Z",
                        "user": {"login": "reviewer"},
                        "head": {"sha": RECENT_SHA},
                        "base": {"ref": "main"},
                        "merge_commit_sha": SECOND_SHA,
                        "labels": [{"name": "incident"}, {"name": "bug"}],
                        "changed_files": 2,
                        "additions": 12,
                        "deletions": 5,
                        "created_at": "2026-07-16T15:00:00Z",
                        "updated_at": "2026-07-16T16:15:00Z",
                    }
                ],
                request=request,
            )
        if path == "/repos/octo-org/pageragent/deployments":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 77,
                        "sha": RECENT_SHA,
                        "ref": "main",
                        "task": "deploy",
                        "environment": "production",
                        "description": "SENTINEL_DEPLOYMENT_BODY",
                        "payload": {"secret": "SENTINEL_PAYLOAD"},
                        "transient_environment": False,
                        "production_environment": True,
                        "created_at": "2026-07-16T16:20:00Z",
                        "updated_at": "2026-07-16T16:21:00Z",
                    }
                ],
                request=request,
            )
        if path == "/repos/octo-org/pageragent/deployments/77/statuses":
            return httpx.Response(
                200,
                json=[
                    {
                        "state": "success",
                        "environment": "production",
                        "description": "SENTINEL_STATUS_BODY",
                        "log_url": "https://evil.example/log",
                        "environment_url": "https://evil.example/env",
                        "created_at": "2026-07-16T16:22:00Z",
                        "updated_at": "2026-07-16T16:22:30Z",
                    }
                ],
                request=request,
            )
        if path == "/repos/octo-org/pageragent/releases":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 91,
                        "tag_name": "v1.2.3",
                        "name": "Checkout release\nSENTINEL_RELEASE_NAME_BODY",
                        "body": "SENTINEL_RAW_RELEASE_BODY",
                        "html_url": "https://evil.example/release",
                        "tarball_url": "https://evil.example/archive",
                        "target_commitish": "main",
                        "author": {"login": "release-bot"},
                        "draft": False,
                        "prerelease": False,
                        "created_at": "2026-07-16T16:00:00Z",
                        "published_at": "2026-07-16T16:20:00Z",
                    }
                ],
                request=request,
            )
        raise AssertionError(f"Unexpected path: {path}")

    limits = GitHubClientLimits(
        request_budget=12,
        max_commits=1,
        max_files_per_commit=2,
        max_pull_requests=1,
        max_deployments=1,
        max_releases=1,
    )
    provider, client = provider_for(handler, limits=limits)
    bundle = provider.collect_evidence(NOW, "checkout-api", RECENT_SHA[:7])

    assert bundle.source_uri == "github://octo-org/pageragent"
    assert bundle.provider == "github_app"
    assert bundle.repository == "octo-org/pageragent"
    assert bundle.provider_version == "github-app-v1"
    assert bundle.active_commit_sha == RECENT_SHA[:7]
    assert len(bundle.commits) == 1
    commit = bundle.commits[0]
    assert len(commit.title) == 300
    assert commit.files_changed == [
        "services/checkout/tests/test_validator.py",
        "services/checkout/validation/payment.py",
    ]
    assert commit.change_types == ["validation_logic", "testing"]
    assert commit.stats.model_dump() == {"additions": 12, "deletions": 5, "total": 17}
    assert commit.diff_summary.startswith("2 files changed (+12/-5)")
    assert bundle.pull_requests[0].labels == ["bug", "incident"]
    assert bundle.pull_requests[0].title == "Fix checkout"
    assert bundle.deployments[0].latest_status is not None
    assert bundle.deployments[0].latest_status.state == "success"
    assert bundle.releases[0].name == "Checkout release"

    serialized = bundle.model_dump_json()
    assert "SENTINEL" not in serialized
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "patch" not in serialized.lower()
    assert not any(
        request.url.path == f"/repos/octo-org/pageragent/commits/{RECENT_SHA[:7]}"
        for request in requested
    )
    list_request = next(
        request
        for request in requested
        if request.url.path == "/repos/octo-org/pageragent/commits"
    )
    assert list_request.url.params["page"] == "1"
    assert list_request.url.params["per_page"] == "1"
    assert list_request.url.params["since"] == "2026-07-16T14:30:00Z"
    client.close()


def test_active_commit_missing_from_recent_page_is_fetched_and_deduplicated() -> None:
    commit_detail_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token(request)
        if path == "/repos/octo-org/pageragent/commits":
            return httpx.Response(200, json=[{"sha": RECENT_SHA}], request=request)
        if path.startswith("/repos/octo-org/pageragent/commits/"):
            commit_detail_paths.append(path)
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=commit_payload(sha), request=request)
        if path in {
            "/repos/octo-org/pageragent/pulls",
            "/repos/octo-org/pageragent/deployments",
            "/repos/octo-org/pageragent/releases",
        }:
            return httpx.Response(200, json=[], request=request)
        raise AssertionError(path)

    provider, client = provider_for(handler)
    bundle = provider.collect_evidence(NOW, "checkout-api", ACTIVE_SHA[:12])

    assert {commit.sha for commit in bundle.commits} == {RECENT_SHA, ACTIVE_SHA[:12]}
    assert commit_detail_paths == [
        f"/repos/octo-org/pageragent/commits/{RECENT_SHA}",
        f"/repos/octo-org/pageragent/commits/{ACTIVE_SHA[:12]}",
    ]
    client.close()


def test_active_commit_replaces_recent_entry_without_exceeding_commit_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token(request)
        if path == "/repos/octo-org/pageragent/commits":
            return httpx.Response(200, json=[{"sha": RECENT_SHA}], request=request)
        if path.startswith("/repos/octo-org/pageragent/commits/"):
            sha = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=commit_payload(sha), request=request)
        if path in {
            "/repos/octo-org/pageragent/pulls",
            "/repos/octo-org/pageragent/deployments",
            "/repos/octo-org/pageragent/releases",
        }:
            return httpx.Response(200, json=[], request=request)
        raise AssertionError(path)

    provider, client = provider_for(
        handler,
        limits=GitHubClientLimits(
            request_budget=10,
            max_commits=1,
            max_pull_requests=1,
            max_deployments=1,
            max_releases=1,
        ),
    )

    bundle = provider.collect_evidence(NOW, "checkout-api", ACTIVE_SHA[:12])

    assert [commit.sha for commit in bundle.commits] == [ACTIVE_SHA[:12]]
    client.close()


def test_optional_active_commit_404_keeps_recent_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token(request)
        if path == "/repos/octo-org/pageragent/commits":
            return httpx.Response(200, json=[{"sha": RECENT_SHA}], request=request)
        if path == f"/repos/octo-org/pageragent/commits/{RECENT_SHA}":
            return httpx.Response(200, json=commit_payload(RECENT_SHA), request=request)
        if path == f"/repos/octo-org/pageragent/commits/{ACTIVE_SHA[:7]}":
            return httpx.Response(
                404,
                json={"message": "SENTINEL_NOT_FOUND_BODY"},
                request=request,
            )
        if path in {
            "/repos/octo-org/pageragent/pulls",
            "/repos/octo-org/pageragent/deployments",
            "/repos/octo-org/pageragent/releases",
        }:
            return httpx.Response(200, json=[], request=request)
        raise AssertionError(path)

    provider, client = provider_for(handler)
    bundle = provider.collect_evidence(NOW, "checkout-api", ACTIVE_SHA[:7])

    assert [commit.sha for commit in bundle.commits] == [RECENT_SHA]
    client.close()


def test_fixture_provider_returns_an_equivalent_deterministic_bundle() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    provider = FixtureGitProvider(repository_root / "scenarios/checkout-commits.json")

    first = provider.collect_evidence(NOW, "checkout-api", "8fa23c1")
    second = provider.collect_evidence(NOW, "checkout-api", "8fa23c1")

    assert first == second
    assert isinstance(first, GitEvidenceBundle)
    assert first.provider == "fixture"
    assert first.source_uri == "fixture://fixture-git-v1"
    assert first.commits == provider.list_recent_commits(NOW)
    assert first.pull_requests == []
    assert first.deployments == []
    assert first.releases == []
    assert first.webhook_receipts == []
    assert first.connector_id is None


def test_bundle_carries_only_hashed_webhook_and_connector_provenance() -> None:
    fixture_provider = FixtureGitProvider(
        Path(__file__).resolve().parents[2] / "scenarios/checkout-commits.json"
    )
    fixture = fixture_provider.collect_evidence(NOW, "checkout-api", "8fa23c1")
    receipt = GitWebhookReceipt(
        delivery_id="11111111-1111-4111-8111-111111111111",
        event_type="deployment_status",
        action=None,
        repository="octo-org/pageragent",
        installation_id=67890,
        connector_version=4,
        credential_version=2,
        body_sha256="d" * 64,
        received_at=NOW,
    )

    enriched = fixture.model_copy(
        update={
            "connector_id": UUID("22222222-2222-4222-8222-222222222222"),
            "connector_version": 4,
            "credential_version": 2,
            "webhook_receipts": [receipt],
        }
    )

    serialized = enriched.model_dump_json()
    assert '"connector_version":4' in serialized
    assert '"credential_version":2' in serialized
    assert '"body_sha256":"' + ("d" * 64) + '"' in serialized
    assert "payload" not in serialized


def test_ranker_safely_matches_short_and_full_sha_prefixes() -> None:
    active_full_sha = "8fa23c1" + ("0" * 33)
    commits = [
        CommitRecord(
            sha="8fa23c1",
            title="Active release",
            author="Maya",
            committed_at=NOW - timedelta(minutes=100),
            services=["checkout-api"],
            owners=["payments-platform"],
            change_types=["validation_logic"],
            files_changed=["services/checkout/validation.py"],
            diff_summary="Update validation logic.",
        ),
        CommitRecord(
            sha=SECOND_SHA,
            title="Recent unrelated commit",
            author="Nolan",
            committed_at=NOW - timedelta(minutes=1),
            services=[],
            owners=[],
            change_types=["observability"],
            files_changed=["telemetry.py"],
            diff_summary="Rename a metric.",
        ),
    ]

    ranked = CommitRanker().rank(
        commits,
        service="checkout-api",
        deployed_at=NOW,
        active_commit_sha=active_full_sha,
        clusters=[],
    )

    active = next(item for item in ranked if item.commit.sha == "8fa23c1")
    assert active.feature_scores["deploy_correlation"] == 1.0
    assert "Matches the commit recorded on the active release." in active.explanation
    assert sha_matches(active_full_sha.upper(), "8FA23C1") is True
    assert sha_matches("", active_full_sha) is False
    assert sha_matches("abc", active_full_sha) is False
    assert sha_matches("../../etc", active_full_sha) is False
