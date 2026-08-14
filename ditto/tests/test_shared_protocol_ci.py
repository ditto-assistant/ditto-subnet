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
@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_shared_protocol_changes_trigger_every_filtered_consumer_workflow(
    workflow_name: str, event: str
) -> None:
    workflow = yaml.load(
        (ROOT / ".depot/workflows" / workflow_name).read_text(),
        Loader=yaml.BaseLoader,
    )

    assert SHARED_PROTOCOL_PATH in workflow["on"][event]["paths"], (
        f"{workflow_name} must run on {SHARED_PROTOCOL_PATH} changes so shared "
        "Pydantic semantics are tested by the consumer that imports them"
    )


def test_root_validator_ci_is_unfiltered() -> None:
    workflow = yaml.load(
        (ROOT / ".depot/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )

    assert not workflow["on"]["pull_request"]
    assert "paths" not in workflow["on"]["push"]
