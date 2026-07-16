import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from app.domain.github import CommitRecord, GitEvidenceBundle
from app.investigation.clustering import ClusterResult
from app.investigation.text import token_coverage, tokenize


class CommitFixture(BaseModel):
    sha: str
    title: str
    author: str
    minutes_before_deploy: int
    services: list[str]
    owners: list[str]
    change_types: list[str]
    files_changed: list[str]
    diff_summary: str


class RankedCommit(BaseModel):
    commit: CommitRecord
    total_score: float
    feature_scores: dict[str, float]
    explanation: list[str]


class GitProvider(Protocol):
    version: str

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle: ...


class FixtureGitProvider:
    version = "fixture-git-v1"

    def __init__(self, fixture_path: Path) -> None:
        self.fixture_path = fixture_path

    def collect_evidence(
        self,
        deployed_at: datetime,
        service: str,
        active_commit_sha: str,
    ) -> GitEvidenceBundle:
        commits = self.list_recent_commits(deployed_at)
        return GitEvidenceBundle(
            source_uri=f"fixture://{self.version}",
            provider="fixture",
            repository=f"fixture/{self.fixture_path.stem[:100]}",
            provider_version=self.version,
            deployed_at=deployed_at,
            service=service,
            active_commit_sha=active_commit_sha,
            commits=commits,
        )

    def list_recent_commits(self, deployed_at: datetime) -> list[CommitRecord]:
        """Compatibility shim for callers migrating to ``collect_evidence``."""

        document = json.loads(self.fixture_path.read_text())
        fixtures = [CommitFixture.model_validate(item) for item in document["commits"]]
        return [
            CommitRecord(
                **fixture.model_dump(exclude={"minutes_before_deploy"}),
                committed_at=deployed_at - timedelta(minutes=fixture.minutes_before_deploy),
            )
            for fixture in fixtures
        ]


class CommitRanker:
    version = "commit-ranker-v1"
    weights = {
        "deploy_correlation": 0.30,
        "service_overlap": 0.25,
        "error_diff_similarity": 0.25,
        "change_risk": 0.10,
        "ownership_relevance": 0.10,
    }
    service_owners = {"checkout-api": "payments-platform"}

    def rank(
        self,
        commits: list[CommitRecord],
        service: str,
        deployed_at: datetime,
        active_commit_sha: str,
        clusters: list[ClusterResult],
    ) -> list[RankedCommit]:
        cluster_terms = [
            term
            for cluster in clusters
            for term in [
                cluster.error_type,
                cluster.endpoint,
                *cluster.affected_attributes.get("payment_methods", []),
            ]
        ]
        query_tokens = tokenize(service, *cluster_terms)
        owner = self.service_owners.get(service)
        ranked: list[RankedCommit] = []

        for commit in commits:
            minutes = abs((deployed_at - commit.committed_at).total_seconds()) / 60
            proximity = max(0.0, 1.0 - minutes / 120)
            deploy_correlation = (
                1.0 if sha_matches(commit.sha, active_commit_sha) else proximity * 0.65
            )
            service_overlap = 1.0 if service in commit.services else 0.0
            document_tokens = tokenize(
                commit.title,
                commit.diff_summary,
                *commit.files_changed,
                *commit.change_types,
            )
            similarity = token_coverage(query_tokens, document_tokens)
            change_risk = self._change_risk(commit.change_types, query_tokens)
            ownership = 1.0 if owner and owner in commit.owners else 0.0
            feature_scores = {
                "deploy_correlation": deploy_correlation,
                "service_overlap": service_overlap,
                "error_diff_similarity": similarity,
                "change_risk": change_risk,
                "ownership_relevance": ownership,
            }
            total_score = sum(
                feature_scores[name] * weight for name, weight in self.weights.items()
            )
            ranked.append(
                RankedCommit(
                    commit=commit,
                    total_score=round(total_score, 4),
                    feature_scores={
                        name: round(score, 4) for name, score in feature_scores.items()
                    },
                    explanation=self._explain(
                        commit,
                        active_commit_sha,
                        service_overlap,
                        similarity,
                        change_risk,
                        ownership,
                    ),
                )
            )

        return sorted(ranked, key=lambda candidate: candidate.total_score, reverse=True)

    @staticmethod
    def _change_risk(change_types: list[str], query_tokens: set[str]) -> float:
        if "validation_logic" in change_types and {"validation", "rule"} & query_tokens:
            return 1.0
        if "configuration" in change_types:
            return 0.55
        if "dependency_update" in change_types:
            return 0.45
        if "observability" in change_types:
            return 0.15
        return 0.25

    @staticmethod
    def _explain(
        commit: CommitRecord,
        active_commit_sha: str,
        service_overlap: float,
        similarity: float,
        change_risk: float,
        ownership: float,
    ) -> list[str]:
        reasons: list[str] = []
        if sha_matches(commit.sha, active_commit_sha):
            reasons.append("Matches the commit recorded on the active release.")
        if service_overlap:
            reasons.append("Touches the affected checkout service.")
        if similarity >= 0.15:
            reasons.append("Changed tokens overlap the clustered validation failure.")
        if change_risk >= 0.8:
            reasons.append("Modifies validation logic on the failing request path.")
        if ownership:
            reasons.append("Owned by the responsible payments platform team.")
        return reasons or ["Weak temporal correlation; no direct failure-path evidence."]


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


def sha_matches(left: str, right: str) -> bool:
    """Match Git SHAs by an unambiguous, minimum-length hexadecimal prefix."""

    if _SHA_PATTERN.fullmatch(left) is None or _SHA_PATTERN.fullmatch(right) is None:
        return False
    normalized_left = left.lower()
    normalized_right = right.lower()
    return normalized_left.startswith(normalized_right) or normalized_right.startswith(
        normalized_left
    )
