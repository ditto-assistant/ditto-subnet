"""Regression checks for immutable CI and release dependencies."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[2] / ".github/workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
PRIVILEGED_TRIGGER = re.compile(
    r"^\s*(pull_request_target|workflow_run|issue_comment|repository_dispatch)\s*:"
)
# Empty on purpose. The stack preview controller was the one approved user of
# pull_request_target; it is now dispatch-only, so nothing in this repository
# may fire on a privileged trigger.
PR_TARGET_ALLOWLIST: frozenset[str] = frozenset()


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
            assert workflow.name in PR_TARGET_ALLOWLIST, (
                f"{workflow.name}:{line_number} uses an unapproved privileged trigger"
            )


def test_the_repository_has_no_privileged_trigger_exceptions() -> None:
    # Re-adding a name above would quietly restore an automatic path to the
    # cloud credentials, so make that edit fail here and be argued for.
    assert not PR_TARGET_ALLOWLIST


def test_stack_preview_is_operator_dispatched_only() -> None:
    text = (WORKFLOWS / "preview-stack.yml").read_text()
    assert "workflow_dispatch:" in text
    assert "pull_request_target:" not in text
    assert "github.event.pull_request" not in text
    assert "environment: preview-stack" in text
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "persist-credentials: false" in text
    assert "checkout@" in text
    # A dispatch can be fired from any ref, and the cloud identity is bound to
    # the environment rather than to a branch, so the controller must refuse to
    # run its own modified copy.
    assert "github.ref == 'refs/heads/main'" in text
    # The PR number is validated before it reaches any script, and the head
    # commit is resolved from the API rather than accepted from the operator --
    # otherwise provision.sh's stale-head check compares an input to itself.
    assert '[[ "$INPUT_PR" =~ ^[1-9][0-9]*$ ]]' in text
    assert 'sha="$(jq -r .head.sha <<<"$pr_json")"' in text
    assert "PREVIEW_SHA: ${{ steps.resolve.outputs.sha }}" in text
    assert "${{ inputs.sha }}" not in text


def test_workflows_do_not_depend_on_blacksmith() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        contents = workflow.read_text().lower()
        assert "blacksmith" not in contents, (
            f"{workflow.name} still depends on Blacksmith capacity or actions"
        )
