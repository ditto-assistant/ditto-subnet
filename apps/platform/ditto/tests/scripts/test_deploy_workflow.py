from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

PLATFORM_ROOT = Path(__file__).parents[3]
MONOREPO_ROOT = PLATFORM_ROOT.parents[1]
MONOREPO_WORKFLOW = MONOREPO_ROOT / ".github" / "workflows" / "platform-deploy.yml"
WORKFLOW_PATH = (
    MONOREPO_WORKFLOW
    if MONOREPO_WORKFLOW.is_file()
    else PLATFORM_ROOT / ".github" / "workflows" / "deploy.yml"
)
RELAY_PATH_FILTER = PLATFORM_ROOT / "scripts" / "relay-runtime-changed.sh"


def _workflow() -> dict:
    if not WORKFLOW_PATH.is_file():
        pytest.skip("deploy workflow is ported by the runtime-deploy stack layer")
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_api_and_relay_releases_have_independent_concurrency_lanes() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]

    assert "concurrency" not in workflow
    assert jobs["deploy"]["concurrency"] == {
        "group": "platform-api-deploy-${{ inputs.environment || 'dev' }}",
        "cancel-in-progress": False,
    }
    assert jobs["relay-release"]["concurrency"] == {
        "group": "platform-relay-release-${{ inputs.environment || 'dev' }}",
        "cancel-in-progress": False,
    }
    assert jobs["relay-build"]["needs"] == "changes"
    assert jobs["deploy"]["needs"] == "changes"
    assert jobs["relay-release"]["needs"] == ["changes", "relay-build", "deploy"]


def test_relay_release_enables_accelerated_iap_uploads() -> None:
    release = _workflow()["jobs"]["relay-release"]
    steps = {step.get("name"): step for step in release["steps"]}

    assert release["env"]["CLOUDSDK_PYTHON_SITEPACKAGES"] == "1"
    acceleration = steps["Accelerate IAP artifact upload"]["run"]
    assert "value(basic.python_location)" in acceleration
    assert "numpy==2.4.4" in acceleration
    assert "import numpy" in acceleration


def _relay_changed(*paths: str) -> bool:
    result = subprocess.run(
        [str(RELAY_PATH_FILTER)],
        input="\n".join(paths),
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_relay_change_filter_ignores_tests_and_dashboard_assets() -> None:
    assert not _relay_changed(
        "dashboard/index.html",
        "ditto/tests/api_server/test_dashboard.py",
    )


def test_relay_change_filter_detects_runtime_and_release_changes() -> None:
    assert _relay_changed("ditto/api_server/inference.py")
    assert _relay_changed("alembic/versions/123_add_column.py")
    assert _relay_changed("uv.lock")
    assert _relay_changed("scripts/deploy-relay-release.sh")
    assert _relay_changed(".github/workflows/deploy.yml")
