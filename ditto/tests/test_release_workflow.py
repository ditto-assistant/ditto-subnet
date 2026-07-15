from pathlib import Path

import yaml

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/release.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/ci.yml"


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
