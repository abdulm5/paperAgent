from pathlib import Path

import yaml

from app.domain.evaluations import ScenarioContract


class ScenarioContractError(ValueError):
    pass


class ScenarioRegistry:
    version = "scenario-registry-v1"

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load_all(self) -> list[ScenarioContract]:
        scenarios: list[ScenarioContract] = []
        for path in sorted(self.directory.glob("*.yaml")):
            if path.name == "suite.yaml":
                continue
            try:
                document = yaml.safe_load(path.read_text())
                scenarios.append(ScenarioContract.model_validate(document))
            except Exception as error:
                raise ScenarioContractError(
                    f"Invalid scenario contract {path.name}: {error}"
                ) from error
        if not scenarios:
            raise ScenarioContractError(f"No scenario contracts found in {self.directory}")
        identifiers = [scenario.id for scenario in scenarios]
        if len(identifiers) != len(set(identifiers)):
            raise ScenarioContractError("Scenario identifiers must be unique")
        return scenarios

    def get(self, scenario_id: str) -> ScenarioContract:
        try:
            return next(item for item in self.load_all() if item.id == scenario_id)
        except StopIteration as error:
            raise ScenarioContractError(f"Unknown scenario: {scenario_id}") from error
