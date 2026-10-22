"""Keep every shared-protocol consumer on the same CI boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SHARED_PROTOCOL_PATH = "packages/ditto-screening-protocol/**"


@pytest.mark.parametrize(
    "workflow_name",
    [
        "platform-ci.yml",
        "backroom-ci.yml",
        "screener-ci.yml",
    ],
)
def test_shared_protocol_changes_trigger_every_filtered_consumer_workflow(
    workflow_name: str,
) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows" / workflow_name).read_text(),
        Loader=yaml.BaseLoader,
    )

    assert set(workflow["on"]) == {"workflow_dispatch"}


def test_root_validator_ci_is_unfiltered() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )

    assert "pull_request" not in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]


# The screening reason-code registry lives in the shared protocol, and each
# side pins itself to it in its own suite, so a registry change must run every
# guard: the registry's invariants, the worker scan, and Platform's tables.
REASON_CODE_GUARDS = {
    "screener-ci.yml": (
        ("packages/ditto-screening-protocol/tests/test_reason_codes.py", None),
        ("workers/screener/tests/test_reason_code_registry.py", "workers/screener/**"),
    ),
    "platform-ci.yml": (
        (
            "apps/platform/ditto/tests/contract/test_screening_reason_codes.py",
            "apps/platform/**",
        ),
    ),
}


@pytest.mark.parametrize("workflow_name", sorted(REASON_CODE_GUARDS))
def test_reason_code_drift_guards_run_when_the_registry_changes(
    workflow_name: str,
) -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows" / workflow_name).read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    for guard, _component_path in REASON_CODE_GUARDS[workflow_name]:
        assert (ROOT / guard).is_file(), guard


def test_screener_ci_runs_the_shared_protocol_suite() -> None:
    """The protocol package's own tests run only in this job."""
    workflow = yaml.load(
        (ROOT / ".github/workflows/screener-ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["protocol-minimum-pydantic"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "python -m pytest packages/ditto-screening-protocol/tests" in commands
