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
CADDYFILE_TEMPLATE = (
    MONOREPO_ROOT
    / "infra"
    / "ansible"
    / "roles"
    / "platform_app"
    / "templates"
    / "Caddyfile.j2"
)
GCP_PLATFORM_TERRAFORM = (
    MONOREPO_ROOT / "infra" / "terraform" / "stacks" / "gcp-platform" / "main.tf"
)


def _workflow() -> dict:
    if not WORKFLOW_PATH.is_file():
        pytest.skip("deploy workflow is ported by the runtime-deploy stack layer")
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_public_proxy_explicitly_denies_runtime_profiles() -> None:
    template = CADDYFILE_TEMPLATE.read_text()

    assert "@runtimeProfiles path /debug/pprof /debug/pprof/*" in template
    assert "respond @runtimeProfiles 404" in template
    for profiler_port in (11000, 11010, 11011, 14434):
        assert str(profiler_port) not in template


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


def test_relay_release_prefers_private_gcs_with_accelerated_iap_fallback() -> None:
    release = _workflow()["jobs"]["relay-release"]
    steps = {step.get("name"): step for step in release["steps"]}

    assert "CLOUDSDK_PYTHON_SITEPACKAGES" not in release["env"]
    assert "Accelerate IAP artifact upload" not in steps

    roll = steps["Roll relay services without shared downtime"]["run"]
    gcs_uri = (
        "gs://ditto-platform-agents-${TARGET_ENVIRONMENT}/relay-releases/"
        "${DEPLOY_REVISION}/${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tgz"
    )
    assert f'artifact_uri="{gcs_uri}"' in roll
    # Staging is a direct objects.insert: gcloud storage cp intermittently
    # preflights with bucket-level objects.list, which the create-only trust
    # model must never grant (it would enumerate agent tarballs).
    assert "gcloud storage cp relay-artifact.tgz" not in roll
    assert 'token="$(gcloud auth print-access-token)"' in roll
    assert "--data-binary @relay-artifact.tgz" in roll
    assert (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        "ditto-platform-agents-${TARGET_ENVIRONMENT}/o?uploadType=media&name=$object"
        in roll
    )
    assert (
        'object="relay-releases%2F${DEPLOY_REVISION}%2F'
        '${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.tgz"' in roll
    )
    assert "--retry 3 --retry-all-errors" in roll
    assert (
        "sudo -iu deploy gcloud storage cp '$artifact_uri' "
        "'$remote_artifact/relay-artifact.tgz'" in roll
    )
    assert "sudo -iu deploy gcloud storage rm '$artifact_uri'" in roll

    # The rollout must remain usable before the Terraform IAM grant is applied.
    # NumPy is installed only on that slow fallback path, never on the GCS path.
    assert "falling back to accelerated IAP upload" in roll
    assert "CLOUDSDK_PYTHON_SITEPACKAGES=1" in roll
    assert "numpy==2.4.4" in roll
    assert "import numpy" in roll
    assert "gcloud compute scp relay-artifact.tgz" in roll
    assert roll.index("--data-binary @relay-artifact.tgz") < roll.index(
        "gcloud compute scp relay-artifact.tgz"
    )


def test_relay_gcs_transport_has_prefix_scoped_create_and_preflight_iam() -> None:
    terraform = GCP_PLATFORM_TERRAFORM.read_text()

    resource = (
        'resource "google_storage_bucket_iam_member" "platform_deploy_relay_create"'
    )
    start = terraform.index(resource)
    grant = terraform[start : terraform.index("\n}\n", start) + 3]

    assert 'role     = "roles/storage.objectCreator"' in grant
    assert "/objects/relay-releases/" in grant
    assert 'role     = "roles/storage.objectAdmin"' not in grant

    read_resource = (
        'resource "google_storage_bucket_iam_member" "platform_deploy_relay_read"'
    )
    read_start = terraform.index(read_resource)
    read_grant = terraform[read_start : terraform.index("\n}\n", read_start) + 3]
    assert 'role     = "roles/storage.objectViewer"' in read_grant
    assert "/objects/relay-releases/" in read_grant
    assert 'role     = "roles/storage.objectAdmin"' not in read_grant
    assert 'matches_prefix = ["relay-releases/"]' in terraform


def test_relay_build_uses_the_self_contained_artifact_builder() -> None:
    build = _workflow()["jobs"]["relay-build"]
    steps = {step.get("name"): step for step in build["steps"]}

    command = steps["Build immutable relay artifact"]["run"]
    assert (
        './scripts/build-relay-release.sh relay-artifact "$DEPLOY_REVISION"' in command
    )
    assert "uv export" not in command

    # The Go relay build needs the Go toolchain, not uv/Python.
    uses = [step.get("uses", "") for step in build["steps"]]
    assert any(entry.startswith("actions/setup-go@") for entry in uses)
    assert not any("setup-uv" in entry for entry in uses)


def _relay_changed(*paths: str) -> bool:
    result = subprocess.run(
        [str(RELAY_PATH_FILTER)],
        input="\n".join(paths),
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_relay_change_filter_ignores_tests_dashboard_and_python_runtime() -> None:
    # The relay runtime is the Go model-relay service; Python Platform runtime
    # changes ship through the ordinary deploy and no longer roll the relay.
    # Every path is REPO-ROOT-relative: the filter's single documented feed is
    # `git diff --name-only` from the monorepo root (release.yml's plan job).
    assert not _relay_changed(
        "apps/platform/dashboard/index.html",
        "apps/platform/ditto/tests/api_server/test_dashboard.py",
        "apps/platform/ditto/api_server/endpoints/inference.py",
        "apps/platform/alembic/versions/123_add_column.py",
        "apps/platform/pyproject.toml",
        "apps/platform/uv.lock",
        "services/model-relay/internal/server/server_test.go",
        "services/model-relay/internal/server/testdata/health.json",
    )


def test_relay_change_filter_rejects_platform_relative_script_paths() -> None:
    # A platform-relative feed (git diff --relative in apps/platform) is the
    # incoherent base this filter used to half-match; it must match NOTHING so
    # a miswired consumer fails loudly in tests rather than silently dropping
    # one class of deploys.
    assert not _relay_changed(
        "scripts/ecosystem.config.js",
        "scripts/build-relay-release.sh",
        "scripts/deploy-relay-release.sh",
    )


def test_relay_change_filter_detects_runtime_and_release_changes() -> None:
    assert _relay_changed("services/model-relay/cmd/model-relay/main.go")
    assert _relay_changed("services/model-relay/internal/server/server.go")
    assert _relay_changed("services/model-relay/go.mod")
    assert _relay_changed("services/model-relay/go.sum")
    assert _relay_changed("apps/platform/scripts/ecosystem.config.js")
    assert _relay_changed("apps/platform/scripts/build-relay-release.sh")
    assert _relay_changed("apps/platform/scripts/deploy-relay-release.sh")
    assert _relay_changed(".github/workflows/platform-deploy.yml")


def test_relay_filter_is_wired_into_the_release_pipeline() -> None:
    # The filter must have a production consumer: release.yml's plan job
    # computes deploy_relay from it, and the deploy_platform call passes the
    # computed value instead of hardcoding true (which rolled both relay slots
    # on dashboard-only releases).
    release_workflow_path = MONOREPO_ROOT / ".github" / "workflows" / "release.yml"
    if not release_workflow_path.is_file():
        pytest.skip("release workflow lives in the monorepo checkout only")
    release_workflow = yaml.safe_load(release_workflow_path.read_text())

    plan_steps = release_workflow["jobs"]["plan"]["steps"]
    relay_step = next(step for step in plan_steps if step.get("id") == "relay")
    assert "relay-runtime-changed.sh" in relay_step["run"]
    assert "git diff --name-only" in relay_step["run"]

    plan_outputs = release_workflow["jobs"]["plan"]["outputs"]
    assert plan_outputs["deploy_relay"] == "${{ steps.relay.outputs.deploy_relay }}"

    deploy_with = release_workflow["jobs"]["deploy_platform"]["with"]
    assert (
        deploy_with["deploy_relay"]
        == "${{ needs.plan.outputs.deploy_relay == 'true' }}"
    )


def test_relay_artifact_is_tarred_before_upload() -> None:
    # actions/upload-artifact does not maintain file permissions, so the
    # tarball must be created BEFORE upload (and shipped as-is afterwards) or
    # the host receives a 0644 model-relay binary.
    workflow = _workflow()
    build = workflow["jobs"]["relay-build"]
    build_steps = {step.get("name"): step for step in build["steps"]}
    build_run = build_steps["Build immutable relay artifact"]["run"]
    assert "tar --create --gzip --file relay-artifact.tgz" in build_run

    upload = next(
        step
        for step in build["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["path"] == "apps/platform/relay-artifact.tgz"

    release = workflow["jobs"]["relay-release"]
    roll = next(
        step
        for step in release["steps"]
        if step.get("name") == "Roll relay services without shared downtime"
    )
    assert "tar --create" not in roll["run"]
    assert "relay-artifact.tgz" in roll["run"]


def test_relay_artifact_is_extracted_as_the_deploy_user() -> None:
    workflow = _workflow()
    release = workflow["jobs"]["relay-release"]
    roll = next(
        step
        for step in release["steps"]
        if step.get("name") == "Roll relay services without shared downtime"
    )["run"]

    create = "sudo install -d -o deploy -g ditto -m 0750 '$remote_artifact'"
    fallback_stage = (
        "sudo install -o deploy -g ditto -m 0640 '$remote_artifact.tgz' "
        "'$remote_artifact/relay-artifact.tgz'"
    )
    gcs_stage = (
        "sudo -iu deploy gcloud storage cp '$artifact_uri' "
        "'$remote_artifact/relay-artifact.tgz'"
    )
    extract = "sudo -u deploy tar --extract --gzip"
    launch = "sudo -iu deploy bash '$remote_artifact/deploy-relay-release.sh'"

    assert create in roll
    assert fallback_stage in roll
    assert gcs_stage in roll
    assert extract in roll
    assert launch in roll
    # Phase markers time the host-side fetch/extract/canary boundaries; the
    # exact chain still proves nothing runs between staging and extraction.
    fetch_start = 'echo \\"relay-phase fetch-start \\$(date -u +%T)\\"'
    fetch_done = 'echo \\"relay-phase fetch-done \\$(date -u +%T)\\"'
    assert (
        f"{create} && {fetch_start} && $remote_fetch && {fetch_done} && {extract}"
        in roll
    )
    assert roll.index(extract) < roll.index(launch)
    assert "mkdir -p '$remote_artifact'" not in roll
    assert "sudo rm -rf -- '$remote_artifact' '$remote_artifact.tgz'" in roll
