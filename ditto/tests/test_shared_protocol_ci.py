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

    assert SHARED_PROTOCOL_PATH in workflow["on"]["pull_request"]["paths"], (
        f"{workflow_name} must run on {SHARED_PROTOCOL_PATH} changes so shared "
        "Pydantic semantics are tested by the consumer that imports them"
    )
    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]


def test_root_validator_ci_is_unfiltered() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )

    assert not workflow["on"]["pull_request"]
    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]
