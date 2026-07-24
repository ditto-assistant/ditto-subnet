import tomllib
from pathlib import Path

import yaml

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION

RELEASE_WORKFLOW_PATH = Path(__file__).parents[2] / ".depot/workflows/release.yml"
CI_WORKFLOW_PATH = Path(__file__).parents[2] / ".depot/workflows/ci.yml"
PYPROJECT_PATH = Path(__file__).parents[2] / "pyproject.toml"


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_release_commits_the_refreshed_project_version_to_uv_lock() -> None:
    config = tomllib.loads(PYPROJECT_PATH.read_text())["tool"]["semantic_release"]
    build_command = config["build_command"]
    assert 'uv lock --upgrade-package "$PACKAGE_NAME"' in build_command
    assert "git add uv.lock" in build_command

    # The full source re-verification (which locks the env) moved out of the old
    # monolithic publish-stack-release job into the parallel verify-source job.
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    verify_steps = workflow["jobs"]["verify-source"]["steps"]
    verification = _step(verify_steps, "Verify the exact release source")
    assert "uv sync --locked --group dev" in verification["run"].splitlines()


def test_public_screener_dependency_needs_no_private_authentication() -> None:
    release_workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    release_steps = release_workflow["jobs"]["release"]["steps"]
    release = _step(release_steps, "Version, tag, and create the GitHub release")
    ci_workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text())
    install = _step(
        ci_workflow["jobs"]["lint-and-test"]["steps"], "Install dependencies"
    )

    assert install == {"name": "Install dependencies", "run": "uv sync --group dev"}
    assert "env" not in release
    for workflow_path in (CI_WORKFLOW_PATH, RELEASE_WORKFLOW_PATH):
        text = workflow_path.read_text()
        assert "DITTO_SCREENER_PROTOCOL_READ_KEY" not in text
        assert "GIT_SSH_COMMAND" not in text
        assert "insteadOf" not in text


def test_validator_release_smokes_each_architecture_before_promotion() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text())
    jobs = workflow["jobs"]

    # The shared heartbeat constant is hoisted to workflow scope; the parallel
    # jobs read it from there instead of each declaring its own copy.
    assert workflow["env"]["HEARTBEAT_PROTOCOL"] == str(HEARTBEAT_PROTOCOL_VERSION)

    # Depot CI currently provides x86_64 sandboxes. Each job still pulls and
    # executes the exact architecture-specific child image before promotion.
    assert jobs["assemble-stack"]["runs-on"] == "depot-ubuntu-latest"
    assert jobs["smoke-validator-arm64"]["runs-on"] == "depot-ubuntu-latest"
    amd64_smoke = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test the amd64 validator artifact by exact child digest",
    )
    arm64_smoke = _step(
        jobs["smoke-validator-arm64"]["steps"],
        "Smoke-test the arm64 validator artifact by exact child digest",
    )
    # Each platform smoke authenticates the requested child and asserts
    # the heartbeat-protocol label matches the release constant.
    assert "--platform linux/amd64" in amd64_smoke["run"]
    assert "--platform linux/arm64" in arm64_smoke["run"]
    for smoke in (amd64_smoke, arm64_smoke):
        assert '"$exact")" = "$HEARTBEAT_PROTOCOL"' in smoke["run"]

    # assemble-stack fans in from the re-verified source and every component
    # image, then builds and cosign-signs the immutable stack descriptor.
    assert set(jobs["assemble-stack"]["needs"]) == {
        "release",
        "verify-source",
        "build-validator",
        "build-sandbox-docker",
        "build-dittobench",
    }
    sign_step = _step(
        jobs["assemble-stack"]["steps"],
        "Smoke-test and authenticate the exact stack descriptor",
    )
    assert 'cosign sign --yes "$exact"' in sign_step["run"]

    # The mutable discovery tag is promoted only after the descriptor is
    # assembled + signed (assemble-stack) AND both validator smokes pass
    # (the amd64 smoke gates assemble-stack; the arm64 smoke gates directly).
    assert jobs["promote-stack-release"]["needs"] == [
        "release",
        "assemble-stack",
        "smoke-validator-arm64",
    ]
