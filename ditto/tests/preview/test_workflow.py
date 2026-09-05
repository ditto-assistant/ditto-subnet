from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
SETUP_NODE = "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"


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
    assert triggers["pull_request"]["types"] == [
        "opened",
        "reopened",
        "synchronize",
        "closed",
    ]
    assert "cheatcodes" in workflow["jobs"]
    assert "dashboard-bundle" in workflow["jobs"]
    assert "dashboard-publish" in workflow["jobs"]
    assert "uv run pytest ditto/tests/preview -q" in text
    assert "uv run python -m ditto.preview compose" in text
    assert "pull-requests: read" in text
    assert "gh api --paginate" in text
    assert "ref: ${{ needs.plan.outputs.sha }}" in text
    assert text.count("CLOUDFLARE_PREVIEW_API_TOKEN") == 1
    assert "actions/upload-artifact@" in text
    assert (
        "node --test apps/platform/dashboard/preview/cloudflare-pages-worker.test.mjs"
        in text
    )
    assert SETUP_NODE in text
    assert "actions/setup-node@820762786026740c76f36085b0efc47a31fe502e" not in text
    assert "workflow_run:" not in text
    assert "pull_request_target:" not in text

    for name in ("plan", "cheatcodes", "dashboard-bundle"):
        job = workflow["jobs"][name]
        assert "environment" not in job
        job_text = yaml.dump(job)
        assert "pull-requests: write" not in job_text
        assert "CLOUDFLARE_API_TOKEN" not in job_text

    dashboard_upload = workflow["jobs"]["dashboard-bundle"]["steps"][-1]["with"]
    assert dashboard_upload["path"] == "preview-artifact"
    assert dashboard_upload["name"] == "dashboard-preview-${{ github.run_attempt }}"
    assert dashboard_upload["overwrite"] is True
    assert not dashboard_upload["path"].startswith(".")

    publish = workflow["jobs"]["dashboard-publish"]
    assert publish["uses"] == "./.github/workflows/preview-dashboard-publish.yml"
    assert publish["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "write",
    }
    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in publish["if"]
    )
    assert publish["with"]["bundle_result"] == "${{ needs.dashboard-bundle.result }}"
    assert publish["with"]["proof_result"] == "${{ needs.cheatcodes.result }}"
    assert publish["with"]["sha"] == "${{ github.event.pull_request.head.sha }}"
    assert publish["secrets"] == {
        "cloudflare_api_token": "${{ secrets.CLOUDFLARE_PREVIEW_API_TOKEN }}"
    }
    assert "inherit" not in publish["secrets"]


def test_stack_preview_controller_caps_slots_and_never_runs_pr_code() -> None:
    text = (ROOT / ".github/workflows/preview-stack.yml").read_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow[True])
    assert triggers["pull_request_target"]["types"] == [
        "opened",
        "reopened",
        "synchronize",
        "closed",
    ]
    control = workflow["jobs"]["control"]
    assert control["environment"] == "preview-stack"
    assert control["steps"][0]["with"]["ref"] == (
        "${{ github.event.repository.default_branch }}"
    )
    assert "ref: ${{ github.event.pull_request.head.sha }}" not in text
    activation = next(
        step
        for step in control["steps"]
        if step.get("name") == "Check whether stack previews are activated"
    )
    assert "GCP_PREVIEW_CONTROLLER_SERVICE_ACCOUNT" in activation["run"]
    assert "GCP_PREVIEW_RUNTIME_SERVICE_ACCOUNT" in activation["run"]
    assert "enabled=$enabled" in activation["run"]
    provision = (ROOT / "preview/cloud/provision.sh").read_text()
    assert "for candidate in {0..7}" in provision
    assert "all 8 preview slots are active" in provision
    assert "--if-generation-match=0" in provision
    assert '"$uri" >/dev/null 2>&1' in provision
    assert '"$(preview_lease_uri "$slot")" >/dev/null' in provision
    assert '--quiet >&2\n\nip="$(gcloud compute instances describe' in provision
    assert '"$(preview_lease_uri "$slot")" >&2' in provision
    assert "head.repo.full_name" in provision
    assert "stale PR head" in provision
    compose = (ROOT / "preview/cloud/compose.yml").read_text()
    runtime = (ROOT / "preview/cloud/runtime.sh").read_text()
    backroom_config = (ROOT / "preview/cloud/backroom.wrangler.jsonc").read_text()
    backroom_entrypoint = (ROOT / "preview/cloud/backroom-entrypoint.sh").read_text()
    assert "backroom: {condition: service_healthy}" in compose
    assert "AbortSignal.timeout(5000)" in compose
    assert "docker compose -f preview/cloud/compose.yml logs" in runtime
    assert '"main": "./dist/server/index.js"' in backroom_config
    assert '"directory": "./dist/client"' in backroom_config
    assert '--env-file "$env_file"' in backroom_entrypoint
    assert "DITTO_ADMIN_API_TOKEN=%s" in backroom_entrypoint
    assert "SESSION_SECRET=%s" in backroom_entrypoint


def test_stack_copy_uses_only_a_sanitized_snapshot_artifact() -> None:
    provision = (ROOT / "preview/cloud/provision.sh").read_text()
    snapshot = (ROOT / ".github/workflows/preview-snapshot.yml").read_text()
    assert "/sanitized/*.dump" in provision
    assert "sign-url" in provision
    assert "--duration=2h" in provision
    assert "sanitize-snapshot.sh" in snapshot
    assert "environment: prod" in snapshot
    export = (ROOT / "preview/cloud/export-snapshot.sh").read_text()
    sanitizer = (ROOT / "preview/cloud/sanitize-snapshot.sh").read_text()
    cloud_compose = (ROOT / "preview/cloud/compose.yml").read_text()
    local_compose = (ROOT / "preview/compose.yml").read_text()
    assert "pg_dump" in export
    assert "--no-owner" in export
    assert "--no-privileges" in export
    assert "ditto_platform_prod" in export
    assert "gcloud compute scp preview/cloud/export-snapshot.sh" in snapshot
    assert 'gcloud compute scp "ditto-pg-platform:${remote_dump}"' in snapshot
    assert "trap cleanup_remote EXIT" in snapshot
    assert "rm -f '$remote_dump' '$remote_script'" in snapshot
    assert "head -c 5" in snapshot
    assert 'pg_dump -Fc --no-owner --no-privileges ditto_platform_prod"' not in snapshot
    assert '/proc/1/comm)" = postgres' in sanitizer
    assert 'docker cp "$source_dump" "$container:/tmp/source.dump"' in sanitizer
    assert 'docker cp "$container:/tmp/sanitized.dump" "$output_dump"' in sanitizer
    assert '-v "$temp_dir:/work"' not in sanitizer
    assert "postgres:17-alpine" in sanitizer
    assert "image: postgres:17-alpine" in cloud_compose
    assert "image: postgres:17-alpine" in local_compose
    assert "postgres:16-alpine" not in sanitizer
    assert "image: postgres:16-alpine" not in cloud_compose
    assert "image: postgres:16-alpine" not in local_compose
    assert "source.dump" in snapshot
    assert "sanitized.dump" in snapshot


def test_trusted_dashboard_publisher_is_read_only_and_exact_sha() -> None:
    text = (ROOT / ".github/workflows/preview-dashboard-publish.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["name"] == "Publish dashboard preview"
    triggers = workflow.get("on", workflow[True])
    assert "workflow_run" not in triggers
    assert "pull_request_target" not in triggers
    assert "workflow_call" in triggers
    inputs = triggers["workflow_call"]["inputs"]
    assert set(inputs) == {
        "action",
        "bundle_result",
        "pr",
        "proof_result",
        "repo",
        "sha",
    }
    assert triggers["workflow_call"]["secrets"] == {
        "cloudflare_api_token": {
            "description": "Pages-only token forwarded explicitly by the caller",
            "required": False,
        }
    }
    assert set(workflow["jobs"]) == {"preflight", "inspect", "publish", "retire"}
    preflight = workflow["jobs"]["preflight"]
    inspect = workflow["jobs"]["inspect"]
    publish = workflow["jobs"]["publish"]
    retire = workflow["jobs"]["retire"]
    # Preflight enters the preview environment only to answer "are the Pages
    # credentials configured", so an unconfigured repository skips publication
    # instead of failing every dashboard PR. It is the most restricted job in
    # the file and must stay that way: no write permission, no third-party
    # action, and no checkout of pull-request code to run beside the token.
    assert preflight["environment"] == "preview"
    assert preflight["permissions"] == {}
    assert "needs" not in preflight
    assert len(preflight["steps"]) == 1
    assert "uses" not in preflight["steps"][0]
    assert preflight["outputs"] == {
        "configured": "${{ steps.check.outputs.configured }}"
    }
    preflight_script = preflight["steps"][0]["run"]
    assert "missing+=(CLOUDFLARE_ACCOUNT_ID)" in preflight_script
    assert "missing+=(CLOUDFLARE_API_TOKEN)" in preflight_script
    assert 'missing_csv="$(IFS=,; echo "${missing[*]}")"' in preflight_script
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
    # Publication still requires inspect's verdict; the credential gate is an
    # additional AND, never a replacement for it.
    assert publish["needs"] == ["inspect", "preflight"]
    assert retire["needs"] == ["inspect", "preflight"]
    for job, mode in ((publish, "publish"), (retire, "retire")):
        condition = " ".join(job["if"].split())
        assert condition == (
            f"needs.inspect.outputs.mode == '{mode}' && "
            "needs.preflight.outputs.configured == 'true'"
        )
    assert "workflow_run:" not in text
    assert "pull_request_target:" not in text
    assert SETUP_NODE in text
    assert "actions/setup-node@820762786026740c76f36085b0efc47a31fe502e" not in text
    checkout = inspect["steps"][0]
    assert "if" not in checkout
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert "trusted preview worker is not on the default branch" in text
    assert "worker-missing-on-default-branch" in text
    assert "retire-prepublish" in text
    assert "retire-closed" in text
    assert "retire-ineligible" in text
    assert "deployment_current" in text
    assert "CURRENT_DEPLOYMENT_URL" in text
    assert "marker='<!-- ditto-dashboard-preview -->'" in text
    retire_steps = retire["steps"]
    existing = retire_steps[0]
    current = retire_steps[1]
    assert existing["id"] == "existing"
    assert existing["name"] == "Check whether this PR ever had a dashboard preview"
    assert current["id"] == "current"
    assert current["if"] == "steps.existing.outputs.found == 'true'"
    assert all(
        "steps.existing.outputs.found == 'true'" in step.get("if", "")
        and "steps.current.outputs.current == 'true'" in step.get("if", "")
        for step in retire_steps[2:]
    )
    assert "PREVIEW_SHA" in text
    assert "BUNDLE_RESULT" in text
    assert "dashboard-preview-${{ github.run_id }}-${{ github.run_attempt }}" in text
    assert (
        "retire-dashboard-preview-${{ github.run_id }}-${{ github.run_attempt }}"
        in text
    )
    assert (
        'if [ "$BUNDLE_RESULT" != success ] || [ "$PROOF_RESULT" != success ]' in text
    )
    assert "manifest.json" in text
    assert "apps/platform/dashboard/preview/cloudflare-pages-worker.mjs" in text
    assert "preview artifact contains a symbolic link" in text
    assert "untrusted artifact supplied reserved Pages control file" in text
    assert '--commit-hash "$PREVIEW_SHA"' in text
    assert '--commit-message "$PUBLISH_MESSAGE"' in text
    assert ".deployment_trigger.metadata.commit_message == $message" in text
    assert "deployment_trigger.metadata.branch" in text
    assert "${endpoint}/${deployment_id}?force=true" in text
    assert ".result_info.total_pages // 1" in text
    assert "per_page=100" not in text
    assert text.count("per_page=20") == 5
    assert "ditto-subnet-dashboard-pr-${{ needs.inspect.outputs.pr }}" in text
    assert "pages project create" in text
    assert "https://${PAGES_PROJECT}.pages.dev" in text
    assert "env=preview" not in text
    assert publish["env"]["PREVIEW_BRANCH"] == "main"
    assert retire["env"]["PREVIEW_BRANCH"] == "main"
    assert "--write-out '%{http_code}'" in text
    assert 'if [ "$http_code" = 200 ] && jq -e' in text
    delete_project = next(
        step
        for step in retire_steps
        if step.get("name") == "Delete the isolated project after PR close"
    )
    assert "needs.inspect.outputs.reason == 'retire-closed'" in delete_project["if"]
    assert 'pages/projects/${PAGES_PROJECT}"' in delete_project["run"]
    assert "-X DELETE" in delete_project["run"]
    assert "This page contains untrusted PR code. Do not enter credentials." in text
    assert "production-public-read-only" in text
    assert 'has("generated_at") and has("miners")' in text
    assert "${{ runner.temp }}/source-artifact" in text
    sanitized_upload = inspect["steps"][-1]["with"]
    assert (
        sanitized_upload["name"]
        == "validated-dashboard-preview-${{ github.run_attempt }}"
    )
    assert sanitized_upload["overwrite"] is True
    assert "pull-requests: write" in text
    assert "environment: prod" not in text
