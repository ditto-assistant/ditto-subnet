"""Regression coverage for the GitHub-hosted to Depot CI cutover."""

from __future__ import annotations

from copy import deepcopy
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


def _platform_parity_view(
    workflow: dict[str, object], *, depot: bool
) -> dict[str, object]:
    """Remove the explicitly tested Depot-only acceleration from parity."""
    workflow = deepcopy(workflow)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    if depot:
        jobs.pop("python-image")

    lint_job = jobs["lint-and-test"]
    assert isinstance(lint_job, dict)
    if depot:
        lint_job.pop("needs")
        lint_job["runs-on"] = "depot-ubuntu-24.04"

    steps = lint_job["steps"]
    assert isinstance(steps, list)
    setup_names = {"Install uv", "Set up Python ${{ matrix.python-version }}"}
    if depot:
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("name") == "Verify locked dependencies":
                step["name"] = "Install dependencies"
            if step.get("uses", "").startswith("actions/checkout@"):
                options = step.get("with")
                if isinstance(options, dict):
                    options.pop("clean", None)
    else:
        steps[:] = [
            step
            for step in steps
            if not isinstance(step, dict) or step.get("name") not in setup_names
        ]
    return workflow


def test_exact_ci_workflow_set_is_cut_over_once() -> None:
    assert {path.name for path in DEPOT_WORKFLOWS.glob("*.yml")} == MIGRATED_WORKFLOWS
    for name in MIGRATED_WORKFLOWS:
        assert not (GITHUB_WORKFLOWS / name).exists()
        assert (GITHUB_WORKFLOWS / f"{name}.disabled").is_file()


def test_depot_workflows_preserve_the_disabled_source_semantics() -> None:
    for name in MIGRATED_WORKFLOWS:
        source = yaml.safe_load((GITHUB_WORKFLOWS / f"{name}.disabled").read_text())
        migrated = yaml.safe_load((DEPOT_WORKFLOWS / name).read_text())
        if name == "platform-ci.yml":
            source = _platform_parity_view(source, depot=False)
            migrated = _platform_parity_view(migrated, depot=True)
        assert _normalized(source) == _normalized(migrated), name


def test_depot_ci_checks_are_secret_free_and_use_depot_runners() -> None:
    for name in MIGRATED_WORKFLOWS:
        text = (DEPOT_WORKFLOWS / name).read_text()
        workflow = yaml.safe_load(text)
        assert "${{ secrets." not in text, name
        for job_name, job in workflow["jobs"].items():
            runner = job.get("runs-on")
            if runner is not None:
                if isinstance(runner, dict):
                    assert runner["size"] in {
                        "2x8",
                        "4x16",
                        "8x32",
                        "16x64",
                        "32x128",
                        "64x256",
                    }
                    assert runner["image"]
                else:
                    assert runner.startswith("depot-ubuntu-"), (name, job_name, runner)


def test_platform_ci_uses_a_reusable_image_and_eight_test_cpus() -> None:
    workflow = yaml.safe_load((DEPOT_WORKFLOWS / "platform-ci.yml").read_text())
    image_job = workflow["jobs"]["python-image"]
    test_job = workflow["jobs"]["lint-and-test"]

    assert image_job["snapshot"]["image-name"] == "ditto-platform-ci"
    assert image_job["snapshot"]["with"]["max-age"] == "3d"
    assert test_job["needs"] == "python-image"
    assert test_job["runs-on"] == {"size": "8x32", "image": "ditto-platform-ci"}
    checkout = next(
        step
        for step in test_job["steps"]
        if "actions/checkout@" in step.get("uses", "")
    )
    assert checkout["with"]["clean"] is False
