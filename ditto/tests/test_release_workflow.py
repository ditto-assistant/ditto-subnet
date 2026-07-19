import tomllib
from pathlib import Path

import yaml

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/ci.yml"
PYPROJECT_PATH = Path(__file__).parents[2] / "pyproject.toml"


def test_release_commits_the_refreshed_project_version_to_uv_lock() -> None:
    config = tomllib.loads(PYPROJECT_PATH.read_text())["tool"]["semantic_release"]
    build_command = config["build_command"]
    assert 'uv lock --upgrade-package "$PACKAGE_NAME"' in build_command
    assert "git add uv.lock" in build_command

    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release_steps = workflow["jobs"]["publish-stack-release"]["steps"]
    verification = next(
        step
        for step in release_steps
        if step.get("name") == "Verify the exact release source"
    )
    assert "uv sync --locked --group dev" in verification["run"].splitlines()


def test_public_screener_dependency_needs_no_private_authentication() -> None:
    release_workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release_steps = release_workflow["jobs"]["release"]["steps"]
    release = next(
        step
        for step in release_steps
        if step.get("name") == "Version, tag, and create the GitHub release"
    )
    ci_workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    install = next(
        step
        for step in ci_workflow["jobs"]["lint-and-test"]["steps"]
        if step.get("name") == "Install dependencies"
    )

    assert install == {"name": "Install dependencies", "run": "uv sync --group dev"}
    assert "env" not in release
    for workflow_path in (CI_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH):
        text = workflow_path.read_text()
        assert "DITTO_SCREENER_PROTOCOL_READ_KEY" not in text
        assert "GIT_SSH_COMMAND" not in text
        assert "insteadOf" not in text


def test_validator_release_smokes_each_architecture_natively_before_promotion() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    assert jobs["publish-stack-release"]["runs-on"] == "blacksmith-4vcpu-ubuntu-2404"
    assert (
        jobs["smoke-validator-arm64"]["runs-on"] == "blacksmith-4vcpu-ubuntu-2404-arm"
    )
    assert jobs["promote-stack-release"]["needs"] == [
        "release",
        "publish-stack-release",
        "smoke-validator-arm64",
    ]
