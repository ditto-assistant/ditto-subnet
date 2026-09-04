"""Regression checks for immutable CI and release dependencies."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[2] / ".github/workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
PRIVILEGED_TRIGGER = re.compile(
    r"^\s*(pull_request_target|workflow_run|issue_comment|repository_dispatch)\s*:"
)
PR_TARGET_ALLOWLIST = {"preview-stack.yml"}


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


def test_untrusted_workflows_do_not_use_privileged_triggers() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = PRIVILEGED_TRIGGER.match(line.split("#", 1)[0])
            if not match:
                continue
            approved_target = (
                match.group(1) == "pull_request_target"
                and workflow.name in PR_TARGET_ALLOWLIST
            )
            assert approved_target, (
                f"{workflow.name}:{line_number} uses an unapproved privileged trigger"
            )


def test_stack_preview_is_the_narrow_trusted_controller_exception() -> None:
    text = (WORKFLOWS / "preview-stack.yml").read_text()
    assert "environment: preview-stack" in text
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "persist-credentials: false" in text
    assert "github.event.pull_request.head.repo.full_name" in text
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in text
    assert "checkout@" in text
    assert "PREVIEW_SHA: ${{ github.event.pull_request.head.sha }}" in text


def test_workflows_do_not_depend_on_blacksmith() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        contents = workflow.read_text().lower()
        assert "blacksmith" not in contents, (
            f"{workflow.name} still depends on Blacksmith capacity or actions"
        )
