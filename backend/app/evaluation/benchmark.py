from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

from app.copilot.actions import derive_action
from app.copilot.citations import CitationValidationError, CitationValidator
from app.copilot.synthesis import DeterministicBriefSynthesizer
from app.domain.evaluations import (
    AdversarialProbeResult,
    EvaluationGate,
    EvaluationScorecard,
    MitigationActionType,
    ScenarioContract,
    ScenarioEvaluationResult,
    ScenarioMetrics,
)
from app.investigation.causes import CauseRanker
from app.investigation.clustering import ErrorClusterer
from app.investigation.commits import CommitRanker, FixtureGitProvider
from app.investigation.runbooks import RunbookRetriever

from .fixtures import build_telemetry_fixture
from .scenario_loader import ScenarioContractError, ScenarioRegistry


class BenchmarkRunner:
    version = "pageragent-benchmark-v1"

    def __init__(
        self,
        scenario_directory: Path,
        commit_fixture_path: Path,
        runbook_directory: Path,
    ) -> None:
        self.scenario_directory = scenario_directory
        self.registry = ScenarioRegistry(scenario_directory)
        self.git_provider = FixtureGitProvider(commit_fixture_path)
        self.commit_ranker = CommitRanker()
        self.cause_ranker = CauseRanker()
        self.clusterer = ErrorClusterer()
        self.runbook_retriever = RunbookRetriever(runbook_directory)
        self.synthesizer = DeterministicBriefSynthesizer()
        self.citation_validator = CitationValidator()

    def run(self) -> EvaluationScorecard:
        suite = yaml.safe_load((self.scenario_directory / "suite.yaml").read_text())
        scenario_by_id = {item.id: item for item in self.registry.load_all()}
        required = [str(value) for value in suite["required_scenarios"]]
        missing = set(required) - set(scenario_by_id)
        if missing:
            raise ScenarioContractError(
                f"Suite references missing scenarios: {', '.join(sorted(missing))}"
            )
        results = [self._evaluate(scenario_by_id[scenario_id]) for scenario_id in required]
        metric_names = list(ScenarioMetrics.model_fields)
        aggregate = {
            name: round(
                sum(getattr(result.metrics, name) for result in results) / len(results), 4
            )
            for name in metric_names
        }
        gates = [
            EvaluationGate(
                metric=str(metric),
                value=aggregate[str(metric)],
                threshold=float(threshold),
                passed=aggregate[str(metric)] >= float(threshold),
            )
            for metric, threshold in suite["aggregate_gates"].items()
        ]
        return EvaluationScorecard(
            suite_version=str(suite["suite_version"]),
            generated_at=datetime.now(UTC),
            passed=all(result.passed for result in results)
            and all(gate.passed for gate in gates),
            scenario_count=len(results),
            aggregate_metrics=aggregate,
            gates=gates,
            scenarios=results,
        )

    def _evaluate(self, scenario: ScenarioContract) -> ScenarioEvaluationResult:
        started = perf_counter()
        telemetry = build_telemetry_fixture(scenario)
        clusters = self.clusterer.cluster(telemetry)
        deployed_at = datetime.fromisoformat(
            str(telemetry["current_release"]["deployed_at"]).replace("Z", "+00:00")
        )
        commits = self.git_provider.list_recent_commits(deployed_at)
        ranked_commits = self.commit_ranker.rank(
            commits=commits,
            service=scenario.service,
            deployed_at=deployed_at,
            active_commit_sha=str(telemetry["current_release"]["commit_sha"]),
            clusters=clusters,
        )
        causes = [
            item.model_copy(update={"evidence_ids": ["e-telemetry", "e-cause"]})
            for item in self.cause_ranker.rank(telemetry, ranked_commits)
        ]
        top_cause = causes[0]
        failure_mode = {
            "release_regression": "elevated-500-errors",
            "upstream_dependency": "upstream-timeouts",
            "feature_flag_regression": "feature-flag-regression",
        }[scenario.simulation.mode]
        query = " ".join(
            [
                scenario.title,
                *(cluster.error_type for cluster in clusters),
                *(str(cluster.affected_attributes) for cluster in clusters),
            ]
        )
        runbooks = self.runbook_retriever.retrieve(
            service=scenario.service,
            failure_mode=failure_mode,
            query=query,
        )
        runbook_ids = [item.document.runbook_id for item in runbooks]
        expected_runbook = scenario.ground_truth.expected_runbook
        runbook_rank = (
            runbook_ids.index(expected_runbook) + 1 if expected_runbook in runbook_ids else 0
        )
        action = derive_action(top_cause)

        impacted_requests = sum(cluster.failure_count for cluster in clusters)
        affected_accuracy = self._affected_attribute_accuracy(scenario, clusters)
        brief, allowed_ids = self._brief(scenario, telemetry, clusters, causes, runbooks)
        citation_coverage = 1.0
        try:
            self.citation_validator.validate(brief, allowed_ids)
        except CitationValidationError:
            citation_coverage = 0.0

        action_safety = float(self._action_matches_truth(action.model_dump(), scenario))
        automation_decision = float(
            action.automation_allowed
            == scenario.ground_truth.expected_action.automation_allowed
        )
        probes = self._run_adversarial_probes(
            scenario, brief, allowed_ids, top_cause
        )
        resilience = sum(float(probe.passed) for probe in probes) / len(probes)
        metrics = ScenarioMetrics(
            cause_top_1=float(
                top_cause.kind is scenario.ground_truth.causal_signal.kind
                and top_cause.reference == scenario.ground_truth.causal_signal.reference
            ),
            runbook_mrr=1 / runbook_rank if runbook_rank else 0.0,
            impact_accuracy=float(
                impacted_requests == scenario.ground_truth.expected_impacted_requests
            ),
            affected_attribute_accuracy=affected_accuracy,
            citation_coverage=citation_coverage,
            action_safety=action_safety,
            automation_decision=automation_decision,
            resilience=resilience,
        )
        thresholds = scenario.thresholds
        passed = (
            metrics.cause_top_1 >= thresholds.cause_top_1
            and metrics.runbook_mrr >= thresholds.runbook_mrr
            and metrics.citation_coverage >= thresholds.citation_coverage
            and metrics.action_safety >= thresholds.action_safety
            and metrics.resilience >= thresholds.resilience
            and metrics.impact_accuracy == 1.0
            and metrics.affected_attribute_accuracy == 1.0
            and metrics.automation_decision == 1.0
        )
        return ScenarioEvaluationResult(
            scenario_id=scenario.id,
            title=scenario.title,
            passed=passed,
            predicted_cause=top_cause,
            predicted_runbook=runbook_ids[0] if runbook_ids else None,
            predicted_action=action.model_dump(mode="json"),
            metrics=metrics,
            adversarial_probes=probes,
            duration_ms=round((perf_counter() - started) * 1_000, 3),
        )

    def _brief(
        self,
        scenario: ScenarioContract,
        telemetry: dict[str, Any],
        clusters: list[Any],
        causes: list[Any],
        runbooks: list[Any],
    ) -> tuple[Any, set[str]]:
        cluster = clusters[0]
        context = {
            "alert": {
                "metric": {
                    "request_count": telemetry["request_count"],
                    "value": telemetry["error_rate"],
                }
            },
            "error_clusters": [
                {
                    **cluster.model_dump(mode="json"),
                    "evidence_ids": ["e-telemetry"],
                }
            ],
            "cause_candidates": [item.model_dump(mode="json") for item in causes],
            "commit_candidates": [],
            "runbook_matches": [
                {
                    "runbook_id": runbooks[0].document.runbook_id,
                    "evidence_ids": ["e-runbook"],
                }
            ],
            "scenario_id": scenario.id,
        }
        return self.synthesizer.generate(context), {
            "e-telemetry",
            "e-cause",
            "e-runbook",
        }

    def _run_adversarial_probes(
        self,
        scenario: ScenarioContract,
        brief: Any,
        allowed_ids: set[str],
        top_cause: Any,
    ) -> list[AdversarialProbeResult]:
        results: list[AdversarialProbeResult] = []
        for case in scenario.adversarial_cases:
            if case == "red_herring_deploy":
                passed = (
                    top_cause.reference
                    not in scenario.ground_truth.red_herring_references
                )
                observation = (
                    "Top cause bypassed the red-herring deployment."
                    if passed
                    else "A red-herring deployment was ranked as causal."
                )
            elif case == "hallucinated_citation":
                invalid = brief.model_copy(deep=True)
                invalid.claims[0].evidence_ids = ["invented-evidence-id"]
                try:
                    self.citation_validator.validate(invalid, allowed_ids)
                    passed = False
                except CitationValidationError:
                    passed = True
                observation = "Unknown evidence identifier was rejected."
            elif case == "missing_evidence":
                missing_cause = self.cause_ranker.rank({}, [])[0]
                passed = derive_action(missing_cause).action_type == "escalate_only"
                observation = "Missing evidence produced a no-write advisory."
            else:
                low_confidence = top_cause.model_copy(update={"score": 0.40})
                passed = derive_action(low_confidence).action_type == "escalate_only"
                observation = "Low-confidence evidence could not unlock automation."
            results.append(
                AdversarialProbeResult(
                    case=case,
                    passed=passed,
                    observation=observation,
                )
            )
        return results

    @staticmethod
    def _affected_attribute_accuracy(
        scenario: ScenarioContract, clusters: list[Any]
    ) -> float:
        observed: dict[str, set[str]] = {}
        for cluster in clusters:
            for key, values in cluster.affected_attributes.items():
                if isinstance(values, list):
                    observed.setdefault(key, set()).update(str(value) for value in values)
        return float(
            all(
                set(expected) <= observed.get(key, set())
                for key, expected in scenario.ground_truth.affected_attributes.items()
            )
        )

    @staticmethod
    def _action_matches_truth(action: dict[str, Any], scenario: ScenarioContract) -> bool:
        truth = scenario.ground_truth.expected_action
        if action["action_type"] != truth.action_type.value:
            return False
        if action["automation_allowed"] != truth.automation_allowed:
            return False
        if truth.action_type is MitigationActionType.ROLLBACK_SERVICE:
            return all(
                action.get(key) == value for key, value in truth.parameters.items()
            )
        if truth.action_type is MitigationActionType.DISABLE_FEATURE_FLAG:
            return action.get("feature_flag") == truth.parameters.get("feature_flag")
        return action.get("target_release") is None
