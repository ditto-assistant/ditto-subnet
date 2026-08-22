from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_preview_workflow_never_publishes_compat_or_prod() -> None:
    text = (ROOT / ".github/workflows/preview.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["name"] == "Preview controls"
    assert workflow["concurrency"]["queue"] == "max"
    assert "--tag" not in text
    assert "compat-2" not in text
    assert "environment: prod" not in text
    triggers = workflow.get("on", workflow[True])
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert "profiles" in dispatch
    assert "cheatcodes" in workflow["jobs"]
    assert "dashboard-bundle" in workflow["jobs"]
    assert "uv run pytest ditto/tests/preview -q" in text
    assert "uv run python -m ditto.preview compose" in text
    assert "pull-requests: read" in text
    assert "pull-requests: write" not in text
    assert "gh api --paginate" in text
    assert "ref: ${{ needs.plan.outputs.sha }}" in text
    assert "CLOUDFLARE_API_TOKEN" not in text
    assert "actions/upload-artifact@" in text
    assert "node --test apps/platform/dashboard/preview/cloudflare-pages-worker.test.mjs" in text

    dashboard_upload = workflow["jobs"]["dashboard-bundle"]["steps"][-1]["with"]
    assert dashboard_upload["path"] == "preview-artifact"
    assert dashboard_upload["name"] == "dashboard-preview-${{ github.run_attempt }}"
    assert dashboard_upload["overwrite"] is True
    assert not dashboard_upload["path"].startswith(".")


def test_trusted_dashboard_publisher_is_read_only_and_exact_sha() -> None:
    text = (ROOT / ".github/workflows/preview-dashboard-publish.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["name"] == "Publish dashboard preview"
    triggers = workflow.get("on", workflow[True])
    assert triggers["workflow_run"]["workflows"] == ["Preview controls"]
    assert triggers["pull_request_target"]["types"] == [
        "opened",
        "reopened",
        "synchronize",
        "closed",
    ]
    assert set(workflow["jobs"]) == {"inspect", "publish", "retire"}
    inspect = workflow["jobs"]["inspect"]
    publish = workflow["jobs"]["publish"]
    retire = workflow["jobs"]["retire"]
    assert "environment" not in inspect
    assert inspect["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "read",
    }
    assert publish["environment"] == "preview"
    assert retire["environment"] == "preview"
    assert publish["concurrency"]["queue"] == "max"
    assert retire["concurrency"]["queue"] == "max"
    assert publish["if"] == "needs.inspect.outputs.mode == 'publish'"
    assert retire["if"] == "needs.inspect.outputs.mode == 'retire'"
    assert "head_repository.full_name == github.repository" in inspect["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in inspect["if"]
    assert "workflow_run.conclusion" not in inspect["if"]
    assert "workflow_run.pull_requests" not in text
    assert "Resolve trusted PR-event retirement" in text
    checkout = inspect["steps"][0]
    assert checkout["if"] == "github.event_name == 'workflow_run'"
    assert "retire-prepublish" in text
    assert "deployment_current" in text
    assert "CURRENT_DEPLOYMENT_URL" in text
    assert 'marker=\'<!-- ditto-dashboard-preview -->\'' in text
    retire_steps = retire["steps"]
    existing = retire_steps[0]
    current = retire_steps[1]
    assert existing["id"] == "existing"
    assert "Check whether this PR ever had a dashboard preview" == existing["name"]
    assert current["id"] == "current"
    assert current["if"] == "steps.existing.outputs.found == 'true'"
    assert all(
        "steps.existing.outputs.found == 'true'" in step.get("if", "")
        and "steps.current.outputs.current == 'true'" in step.get("if", "")
        for step in retire_steps[2:]
    )
    assert "RUN_HEAD_SHA" in text
    assert "SOURCE_CONCLUSION" in text
    assert "SOURCE_RUN_ATTEMPT" in text
    assert "dashboard-preview-${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert "retire-dashboard-preview-${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert 'if [ "$SOURCE_CONCLUSION" != success ]' in text
    assert "manifest.json" in text
    assert "apps/platform/dashboard/preview/cloudflare-pages-worker.mjs" in text
    assert "preview artifact contains a symbolic link" in text
    assert "untrusted artifact supplied reserved Pages control file" in text
    assert "--commit-hash \"$PREVIEW_SHA\"" in text
    assert '--commit-message "$PUBLISH_MESSAGE"' in text
    assert ".deployment_trigger.metadata.commit_message == $message" in text
    assert "deployment_trigger.metadata.branch" in text
    assert "${endpoint}/${deployment_id}?force=true" in text
    assert ".result_info.total_pages // 1" in text
    assert "jq -r .url" in text
    assert "This page contains untrusted PR code. Do not enter credentials." in text
    assert "production-public-read-only" in text
    assert 'has("generated_at") and has("miners")' in text
    assert "${{ runner.temp }}/source-artifact" in text
    sanitized_upload = inspect["steps"][-1]["with"]
    assert sanitized_upload["name"] == "validated-dashboard-preview-${{ github.run_attempt }}"
    assert sanitized_upload["overwrite"] is True
    assert "pull-requests: write" in text
    assert "environment: prod" not in text
