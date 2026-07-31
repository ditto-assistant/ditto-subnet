"""Regression checks for immutable CI and release dependencies."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[2] / ".github/workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_remote_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        for line in workflow.read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
                continue
            action = stripped.split("uses:", 1)[1].strip().split()[0]
            if action.startswith("./"):
                continue
            _, separator, revision = action.rpartition("@")
            assert separator and FULL_SHA.fullmatch(revision), (
                f"{workflow.name} must pin {action!r} to a full commit SHA"
            )


def test_uv_sync_is_locked_in_workflows() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            if "uv sync" not in line:
                continue
            assert "--locked" in line or "--frozen" in line, (
                f"{workflow.name}:{line_number} runs uv sync without a lock guard"
            )
