from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha = str
ServiceName = Annotated[str, Field(min_length=1, max_length=200)]
ActorName = Annotated[str, Field(min_length=1, max_length=200)]
ChangeType = Annotated[str, Field(min_length=1, max_length=100)]
FilePath = Annotated[str, Field(min_length=1, max_length=500)]
LabelName = Annotated[str, Field(min_length=1, max_length=100)]
RepositoryName = Annotated[
    str,
    Field(
        min_length=3,
        max_length=201,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$",
    ),
]


class GitHubEvidenceModel(BaseModel):
    """Base contract for bounded, persistence-safe GitHub evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GitCommitStats(GitHubEvidenceModel):
    additions: int = Field(default=0, ge=0, le=1_000_000_000)
    deletions: int = Field(default=0, ge=0, le=1_000_000_000)
    total: int = Field(default=0, ge=0, le=2_000_000_000)


class GitCommitFile(GitHubEvidenceModel):
    path: FilePath
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed"]
    additions: int = Field(default=0, ge=0, le=1_000_000_000)
    deletions: int = Field(default=0, ge=0, le=1_000_000_000)
    changes: int = Field(default=0, ge=0, le=2_000_000_000)


class CommitRecord(GitHubEvidenceModel):
    sha: Sha = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    title: str = Field(min_length=1, max_length=300)
    author: str = Field(min_length=1, max_length=200)
    committed_at: datetime
    services: list[ServiceName] = Field(default_factory=list, max_length=16)
    owners: list[ActorName] = Field(default_factory=list, max_length=16)
    change_types: list[ChangeType] = Field(default_factory=list, max_length=16)
    files_changed: list[FilePath] = Field(default_factory=list, max_length=100)
    diff_summary: str = Field(default="No file-level summary available.", max_length=1_000)
    stats: GitCommitStats = Field(default_factory=GitCommitStats)
    files: list[GitCommitFile] = Field(default_factory=list, max_length=100)


class GitPullRequest(GitHubEvidenceModel):
    number: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    state: Literal["open", "closed"]
    merged: bool
    author: str = Field(min_length=1, max_length=200)
    head_sha: Sha | None = Field(
        default=None,
        min_length=7,
        max_length=64,
        pattern=r"^[0-9a-fA-F]+$",
    )
    merge_commit_sha: Sha | None = Field(
        default=None,
        min_length=7,
        max_length=64,
        pattern=r"^[0-9a-fA-F]+$",
    )
    base_ref: str = Field(default="", max_length=200)
    labels: list[LabelName] = Field(default_factory=list, max_length=20)
    changed_files: int = Field(default=0, ge=0, le=1_000_000)
    additions: int = Field(default=0, ge=0, le=1_000_000_000)
    deletions: int = Field(default=0, ge=0, le=1_000_000_000)
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None = None


class GitDeploymentStatus(GitHubEvidenceModel):
    state: Literal[
        "error",
        "failure",
        "inactive",
        "in_progress",
        "pending",
        "queued",
        "success",
        "unknown",
    ]
    environment: str = Field(default="", max_length=200)
    created_at: datetime
    updated_at: datetime


class GitDeployment(GitHubEvidenceModel):
    deployment_id: int = Field(gt=0)
    sha: Sha = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    ref: str = Field(default="", max_length=200)
    task: str = Field(default="", max_length=100)
    environment: str = Field(default="", max_length=200)
    transient_environment: bool = False
    production_environment: bool = False
    created_at: datetime
    updated_at: datetime
    latest_status: GitDeploymentStatus | None = None


class GitRelease(GitHubEvidenceModel):
    release_id: int = Field(gt=0)
    tag_name: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=300)
    target_commitish: str = Field(default="", max_length=200)
    author: str = Field(min_length=1, max_length=200)
    draft: bool = False
    prerelease: bool = False
    created_at: datetime
    published_at: datetime | None = None


class GitWebhookReceipt(GitHubEvidenceModel):
    """Minimal webhook provenance; the signed provider payload is never persisted."""

    delivery_id: str = Field(min_length=1, max_length=200)
    event_type: Literal[
        "push",
        "pull_request",
        "deployment",
        "deployment_status",
        "release",
    ]
    action: str | None = Field(default=None, max_length=100)
    repository: RepositoryName
    installation_id: int = Field(gt=0)
    connector_version: int = Field(gt=0)
    credential_version: int = Field(gt=0)
    body_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    received_at: datetime


class GitEvidenceBundle(GitHubEvidenceModel):
    source_uri: str = Field(min_length=1, max_length=500)
    provider: Literal["fixture", "github_app"]
    repository: RepositoryName
    provider_version: str = Field(min_length=1, max_length=100)
    connector_id: UUID | None = None
    connector_version: int | None = Field(default=None, gt=0)
    credential_version: int | None = Field(default=None, gt=0)
    deployed_at: datetime
    service: str = Field(min_length=1, max_length=200)
    active_commit_sha: Sha = Field(
        min_length=7,
        max_length=64,
        pattern=r"^[0-9a-fA-F]+$",
    )
    commits: list[CommitRecord] = Field(default_factory=list, max_length=100)
    pull_requests: list[GitPullRequest] = Field(default_factory=list, max_length=50)
    deployments: list[GitDeployment] = Field(default_factory=list, max_length=50)
    releases: list[GitRelease] = Field(default_factory=list, max_length=50)
    webhook_receipts: list[GitWebhookReceipt] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_source(self) -> "GitEvidenceBundle":
        if self.provider == "github_app":
            expected = f"github://{self.repository}"
            if self.source_uri != expected:
                raise ValueError("GitHub evidence source must match its repository")
        elif not self.source_uri.startswith("fixture://"):
            raise ValueError("Fixture evidence must use a fixture source URI")
        return self
