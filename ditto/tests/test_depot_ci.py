"""Regression coverage for the GitHub-hosted to Depot CI cutover."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
GITHUB_WORKFLOWS = ROOT / ".github/workflows"
DEPOT_WORKFLOWS = ROOT / ".depot/workflows"
MIGRATED_WORKFLOWS = {
    "backroom-ci.yml",
    "ci.yml",
    "conventional-pr.yml",
    "datagen-ci.yml",
    "dittobench.yml",
    "infra-ci.yml",
    "model-relay.yml",
    "platform-ci.yml",
    "platform-migration-order.yml",
    "screener-ci.yml",
    "screener-core-e2e.yml",
    "starter-kit-ci.yml",
}


def _normalized(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if not isinstance(value, str):
        return value
    if value == "ubuntu-latest":
        return "depot-ubuntu-latest"
    if value == "ubuntu-24.04":
        return "depot-ubuntu-24.04"
    return value.replace(".github/", ".depot/")


def test_exact_ci_workflow_set_is_cut_over_once() -> None:
    assert {path.name for path in DEPOT_WORKFLOWS.glob("*.yml")} == MIGRATED_WORKFLOWS
    for name in MIGRATED_WORKFLOWS:
        assert not (GITHUB_WORKFLOWS / name).exists()
        assert (GITHUB_WORKFLOWS / f"{name}.disabled").is_file()


def test_depot_workflows_preserve_the_disabled_source_semantics() -> None:
    for name in MIGRATED_WORKFLOWS:
        source = yaml.safe_load((GITHUB_WORKFLOWS / f"{name}.disabled").read_text())
        migrated = yaml.safe_load((DEPOT_WORKFLOWS / name).read_text())
        assert _normalized(source) == _normalized(migrated), name


def test_depot_ci_checks_are_secret_free_and_use_depot_runners() -> None:
    for name in MIGRATED_WORKFLOWS:
        text = (DEPOT_WORKFLOWS / name).read_text()
        workflow = yaml.safe_load(text)
        assert "${{ secrets." not in text, name
        for job_name, job in workflow["jobs"].items():
            runner = job.get("runs-on")
            if runner is not None:
                assert runner.startswith("depot-ubuntu-"), (name, job_name, runner)
