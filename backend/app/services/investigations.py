import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.db.models import (
    CommitCandidateRecord,
    ErrorClusterRecord,
    EvidenceArtifactRecord,
    IncidentEventRecord,
    IncidentRecord,
    InvestigationRunRecord,
    RunbookMatchRecord,
)
from app.domain.incidents import AlertPayload
from app.domain.investigations import (
    CommitCandidate,
    ErrorCluster,
    EvidenceArtifact,
    InvestigationDetail,
    InvestigationStatus,
    RunbookMatch,
    RunbookSection,
)
from app.investigation.clustering import ErrorClusterer
from app.investigation.collectors import HttpTelemetryCollector, TelemetryCollector
from app.investigation.commits import CommitRanker, FixtureGitProvider, GitProvider
from app.investigation.runbooks import RunbookRetriever
from app.investigation.text import canonical_hash
from app.services.incidents import IncidentNotFoundError


class InvestigationNotFoundError(Exception):
    pass


class InvestigationExecutionError(Exception):
    pass


class InvestigationService:
    def __init__(
        self,
        session: Session,
        collector: TelemetryCollector,
        git_provider: GitProvider,
        clusterer: ErrorClusterer,
        commit_ranker: CommitRanker,
        runbook_retriever: RunbookRetriever,
    ) -> None:
        self.session = session
        self.collector = collector
        self.git_provider = git_provider
        self.clusterer = clusterer
        self.commit_ranker = commit_ranker
        self.runbook_retriever = runbook_retriever

    def run(self, incident_id: UUID) -> InvestigationDetail:
        incident = self._load_incident(incident_id)
        if not incident.alerts:
            raise InvestigationExecutionError("Incident has no source alert")
        alert = AlertPayload.model_validate(incident.alerts[0].payload)
        preflight_hash = canonical_hash(
            {
                "alert": alert.model_dump(mode="json"),
                "collector": self.collector.version,
                "clusterer": self.clusterer.version,
                "ranker": self.commit_ranker.version,
                "retriever": self.runbook_retriever.version,
                "git_provider": self.git_provider.version,
            }
        )
        run = InvestigationRunRecord(
            incident_id=incident.id,
            status=InvestigationStatus.RUNNING.value,
            collector_version=self.collector.version,
            clusterer_version=self.clusterer.version,
            ranker_version=self.commit_ranker.version,
            retrieval_version=self.runbook_retriever.version,
            input_hash=preflight_hash,
        )
        self.session.add(run)
        self.session.commit()

        try:
            telemetry = self.collector.collect(str(alert.telemetry_url))
            telemetry_artifact = self._add_artifact(
                run.id,
                kind="telemetry_snapshot",
                source_uri=str(alert.telemetry_url),
                payload=telemetry,
            )
            deployment_artifact = self._add_artifact(
                run.id,
                kind="deployment_history",
                source_uri=f"{str(alert.telemetry_url)}#deployments",
                payload={
                    "current_release": telemetry.get("current_release", {}),
                    "deployments": telemetry.get("deployments", []),
                },
            )
            self.session.flush()

            cluster_results = self.clusterer.cluster(telemetry)
            cluster_records = [
                ErrorClusterRecord(
                    investigation_id=run.id,
                    signature=cluster.signature,
                    error_type=cluster.error_type,
                    endpoint=cluster.endpoint,
                    affected_attributes=cluster.affected_attributes,
                    failure_count=cluster.failure_count,
                    first_seen_at=cluster.first_seen_at,
                    last_seen_at=cluster.last_seen_at,
                    sample_request_ids=cluster.sample_request_ids,
                    evidence_ids=[str(telemetry_artifact.id)],
                )
                for cluster in cluster_results
            ]
            self.session.add_all(cluster_records)
            self.session.flush()

            current_release = telemetry.get("current_release", {})
            deployed_at = self._parse_datetime(
                current_release.get("deployed_at") or alert.release.deployed_at
            )
            active_commit_sha = str(
                current_release.get("commit_sha") or alert.release.commit_sha
            )
            commits = self.git_provider.list_recent_commits(deployed_at)
            commit_catalog = self._add_artifact(
                run.id,
                kind="commit_catalog",
                source_uri=f"fixture://{self.git_provider.version}",
                payload={
                    "provider_version": self.git_provider.version,
                    "commits": [commit.model_dump(mode="json") for commit in commits],
                },
            )
            self.session.flush()
            ranked_commits = self.commit_ranker.rank(
                commits=commits,
                service=incident.service,
                deployed_at=deployed_at,
                active_commit_sha=active_commit_sha,
                clusters=cluster_results,
            )
            common_evidence_ids = [
                str(telemetry_artifact.id),
                str(deployment_artifact.id),
                str(commit_catalog.id),
                *(str(cluster.id) for cluster in cluster_records),
            ]
            for rank, candidate in enumerate(ranked_commits[:3], start=1):
                self.session.add(
                    CommitCandidateRecord(
                        investigation_id=run.id,
                        commit_sha=candidate.commit.sha,
                        rank=rank,
                        total_score=candidate.total_score,
                        title=candidate.commit.title,
                        author=candidate.commit.author,
                        committed_at=candidate.commit.committed_at,
                        files_changed=candidate.commit.files_changed,
                        diff_summary=candidate.commit.diff_summary,
                        feature_scores=candidate.feature_scores,
                        explanation=candidate.explanation,
                        evidence_ids=common_evidence_ids,
                    )
                )

            failure_mode = self._failure_mode(alert)
            query = self._investigation_query(incident.summary, cluster_results)
            runbook_matches = self.runbook_retriever.retrieve(
                service=incident.service,
                failure_mode=failure_mode,
                query=query,
            )
            runbook_corpus = self._add_artifact(
                run.id,
                kind="runbook_corpus",
                source_uri="file://runbooks",
                payload={
                    "retrieval_version": self.runbook_retriever.version,
                    "ranked_runbooks": [
                        {
                            "runbook_id": match.document.runbook_id,
                            "title": match.document.title,
                            "service": match.document.service,
                            "failure_mode": match.document.failure_mode,
                            "content": match.document.content,
                            "content_hash": match.document.content_hash,
                            "score": match.total_score,
                        }
                        for match in runbook_matches
                    ],
                },
            )
            self.session.flush()
            for rank, match in enumerate(runbook_matches[:3], start=1):
                self.session.add(
                    RunbookMatchRecord(
                        investigation_id=run.id,
                        runbook_id=match.document.runbook_id,
                        rank=rank,
                        title=match.document.title,
                        service=match.document.service,
                        failure_mode=match.document.failure_mode,
                        total_score=match.total_score,
                        score_breakdown=match.score_breakdown,
                        matched_sections=match.matched_sections,
                        content_hash=match.document.content_hash,
                        evidence_ids=[
                            str(telemetry_artifact.id),
                            str(runbook_corpus.id),
                            *(str(cluster.id) for cluster in cluster_records),
                        ],
                    )
                )

            run.input_hash = canonical_hash(
                {
                    "preflight": preflight_hash,
                    "artifact_hashes": sorted(
                        [
                            telemetry_artifact.content_hash,
                            deployment_artifact.content_hash,
                            commit_catalog.content_hash,
                            runbook_corpus.content_hash,
                        ]
                    ),
                }
            )
            run.status = InvestigationStatus.COMPLETED.value
            run.completed_at = datetime.now(UTC)
            top_commit = ranked_commits[0] if ranked_commits else None
            top_runbook = runbook_matches[0] if runbook_matches else None
            self.session.add(
                IncidentEventRecord(
                    incident_id=incident.id,
                    event_type="investigation.completed",
                    actor="pageragent-investigator",
                    from_status=None,
                    to_status=incident.status,
                    note="Evidence collection and deterministic ranking completed.",
                    payload={
                        "investigation_id": str(run.id),
                        "top_commit": top_commit.commit.sha if top_commit else None,
                        "top_runbook": (
                            top_runbook.document.runbook_id if top_runbook else None
                        ),
                        "error_cluster_count": len(cluster_records),
                    },
                )
            )
            self.session.commit()
            return self.get(run.id)
        except Exception as error:
            self.session.rollback()
            failed_run = self.session.get(InvestigationRunRecord, run.id)
            if failed_run is not None:
                failed_run.status = InvestigationStatus.FAILED.value
                failed_run.failure_reason = str(error)[:2_000]
                failed_run.completed_at = datetime.now(UTC)
                self.session.commit()
            raise InvestigationExecutionError(str(error)) from error

    def get_latest(self, incident_id: UUID) -> InvestigationDetail:
        if self.session.get(IncidentRecord, incident_id) is None:
            raise IncidentNotFoundError
        run = self.session.scalar(
            select(InvestigationRunRecord)
            .where(InvestigationRunRecord.incident_id == incident_id)
            .order_by(InvestigationRunRecord.started_at.desc())
            .limit(1)
        )
        if run is None:
            raise InvestigationNotFoundError
        return self.get(run.id)

    def get(self, investigation_id: UUID) -> InvestigationDetail:
        run = self.session.scalar(
            select(InvestigationRunRecord)
            .where(InvestigationRunRecord.id == investigation_id)
            .options(
                selectinload(InvestigationRunRecord.evidence),
                selectinload(InvestigationRunRecord.error_clusters),
                selectinload(InvestigationRunRecord.commit_candidates),
                selectinload(InvestigationRunRecord.runbook_matches),
            )
        )
        if run is None:
            raise InvestigationNotFoundError
        return self._to_detail(run)

    def _load_incident(self, incident_id: UUID) -> IncidentRecord:
        incident = self.session.scalar(
            select(IncidentRecord)
            .where(IncidentRecord.id == incident_id)
            .options(selectinload(IncidentRecord.alerts))
        )
        if incident is None:
            raise IncidentNotFoundError
        return incident

    def _add_artifact(
        self,
        investigation_id: UUID,
        kind: str,
        source_uri: str,
        payload: dict[str, Any],
    ) -> EvidenceArtifactRecord:
        artifact = EvidenceArtifactRecord(
            investigation_id=investigation_id,
            kind=kind,
            source_uri=source_uri,
            content_hash=canonical_hash(payload),
            payload=payload,
        )
        self.session.add(artifact)
        return artifact

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    @staticmethod
    def _failure_mode(alert: AlertPayload) -> str:
        if "error_rate" in alert.metric.name:
            return "elevated-500-errors"
        return alert.metric.name.replace("_", "-")

    @staticmethod
    def _investigation_query(summary: str, clusters: list[Any]) -> str:
        parts = [summary]
        for cluster in clusters:
            parts.extend(
                [
                    cluster.error_type,
                    cluster.endpoint,
                    json.dumps(cluster.affected_attributes, sort_keys=True),
                ]
            )
        return " ".join(parts)

    @staticmethod
    def _to_detail(run: InvestigationRunRecord) -> InvestigationDetail:
        return InvestigationDetail(
            id=run.id,
            incident_id=run.incident_id,
            status=InvestigationStatus(run.status),
            collector_version=run.collector_version,
            clusterer_version=run.clusterer_version,
            ranker_version=run.ranker_version,
            retrieval_version=run.retrieval_version,
            input_hash=run.input_hash,
            failure_reason=run.failure_reason,
            started_at=run.started_at,
            completed_at=run.completed_at,
            evidence=[
                EvidenceArtifact(
                    id=item.id,
                    kind=item.kind,
                    source_uri=item.source_uri,
                    content_hash=item.content_hash,
                    payload=item.payload,
                    collected_at=item.collected_at,
                )
                for item in run.evidence
            ],
            error_clusters=[
                ErrorCluster(
                    id=item.id,
                    signature=item.signature,
                    error_type=item.error_type,
                    endpoint=item.endpoint,
                    affected_attributes=item.affected_attributes,
                    failure_count=item.failure_count,
                    first_seen_at=item.first_seen_at,
                    last_seen_at=item.last_seen_at,
                    sample_request_ids=item.sample_request_ids,
                    evidence_ids=item.evidence_ids,
                )
                for item in run.error_clusters
            ],
            commit_candidates=[
                CommitCandidate(
                    id=item.id,
                    commit_sha=item.commit_sha,
                    rank=item.rank,
                    total_score=item.total_score,
                    title=item.title,
                    author=item.author,
                    committed_at=item.committed_at,
                    files_changed=item.files_changed,
                    diff_summary=item.diff_summary,
                    feature_scores=item.feature_scores,
                    explanation=item.explanation,
                    evidence_ids=item.evidence_ids,
                )
                for item in run.commit_candidates
            ],
            runbook_matches=[
                RunbookMatch(
                    id=item.id,
                    runbook_id=item.runbook_id,
                    rank=item.rank,
                    title=item.title,
                    service=item.service,
                    failure_mode=item.failure_mode,
                    total_score=item.total_score,
                    score_breakdown=item.score_breakdown,
                    matched_sections=[
                        RunbookSection.model_validate(section)
                        for section in item.matched_sections
                    ],
                    content_hash=item.content_hash,
                    evidence_ids=item.evidence_ids,
                )
                for item in run.runbook_matches
            ],
        )


def build_investigation_service(session: Session) -> InvestigationService:
    return InvestigationService(
        session=session,
        collector=HttpTelemetryCollector(settings.investigation_http_timeout_seconds),
        git_provider=FixtureGitProvider(settings.commit_fixture_path),
        clusterer=ErrorClusterer(),
        commit_ranker=CommitRanker(),
        runbook_retriever=RunbookRetriever(settings.runbook_directory),
    )
