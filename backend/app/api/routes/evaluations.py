from fastapi import APIRouter, Depends

from app.auth.dependencies import require_permission
from app.core.config import settings
from app.domain.auth import Permission, Principal
from app.domain.evaluations import EvaluationScorecard, ScenarioContract
from app.evaluation.benchmark import BenchmarkRunner
from app.evaluation.scenario_loader import ScenarioRegistry

router = APIRouter(prefix="/evaluations", tags=["evaluations"])


def build_benchmark_runner() -> BenchmarkRunner:
    return BenchmarkRunner(
        scenario_directory=settings.scenario_directory,
        commit_fixture_path=settings.commit_fixture_path,
        runbook_directory=settings.runbook_directory,
    )


@router.get("/scenarios", response_model=list[ScenarioContract])
def list_scenarios(
    _principal: Principal = Depends(require_permission(Permission.EVALUATIONS_RUN)),
) -> list[ScenarioContract]:
    return ScenarioRegistry(settings.scenario_directory).load_all()


@router.get("/scorecard", response_model=EvaluationScorecard)
def get_scorecard(
    _principal: Principal = Depends(require_permission(Permission.EVALUATIONS_RUN)),
) -> EvaluationScorecard:
    return build_benchmark_runner().run()
