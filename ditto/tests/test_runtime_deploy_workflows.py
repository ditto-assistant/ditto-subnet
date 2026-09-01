import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow[True])


def test_manual_platform_deploy_requires_release_tag_or_force() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    )
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]
    contract = workflow["jobs"]["changes"]["steps"][1]["run"]

    assert dispatch["force"]["default"] is False
    assert "git tag --points-at" in contract
    assert '"$FORCE_DEPLOY" != true' in contract
    assert "[0-9]+$'" in contract


def test_manual_screener_deploy_requires_release_tag_or_force() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/screener-deploy.yml").read_text()
    )
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]
    resolve = next(
        step
        for step in workflow["jobs"]["discover"]["steps"]
        if step.get("name") == "Resolve the immutable released revision"
    )

    assert dispatch["force"]["default"] is False
    assert "git tag --points-at" in resolve["run"]
    assert '"$FORCE_DEPLOY" != true' in resolve["run"]
    assert "[0-9]+$'" in resolve["run"]


def _platform_deploy_command() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    return step["run"]


def _platform_checkout_root() -> str:
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    return defaults["platform_checkout_root"]


def test_platform_deploy_targets_the_ansible_provisioned_checkout() -> None:
    """The deploy path and the path Ansible provisions are one fact, not two.

    They drifted once already: the workflow shipped pointing at a directory the
    prod host did not have, so every `apps/platform` change merged here looked
    deployed and reached nothing. Pinning them together means a future move of
    `platform_checkout_root` fails here instead of in production.
    """
    root = _platform_checkout_root()
    assert root.startswith("/")
    assert f"cd {root} &&" in _platform_deploy_command()


def test_platform_deploy_preflights_the_checkout_before_touching_the_host() -> None:
    """An unconverged host must be diagnosable from the failure alone.

    A bare `cd` into a missing directory reports only that the directory is
    missing -- not that the host was never provisioned, not that it is still
    serving the pre-cutover checkout, and not which playbook fixes it.
    """
    command = _platform_deploy_command()
    root = _platform_checkout_root()

    # The guard runs BEFORE the cd it protects, or it protects nothing.
    assert command.index(f"[ ! -d {root}/.git ]") < command.index(f"cd {root} &&")
    # Names the pre-cutover checkout, so "merged but not live" is self-evident.
    assert "/opt/ditto-platform/.git" in command
    # Names the fix, not just the symptom.
    assert "gcp-platform-app.yml" in command
    # EX_CONFIG: a provisioning gap is not a deploy fault.
    assert "exit 78" in command


def test_platform_deploy_rejects_a_half_provisioned_checkout() -> None:
    """A cloned checkout with no rendered .env must not read as deployable.

    A converge that aborts between cloning and rendering leaves `.git` present
    and `.env` absent. Guarding only on `.git` waves that straight through into
    an app that cannot boot, and unlike "never provisioned" it looks ready.
    """
    command = _platform_deploy_command()
    root = _platform_checkout_root()

    assert f"[ ! -s {root}/apps/platform/.env ]" in command
    # -s, not -f: a zero-byte .env is as unbootable as a missing one.
    assert f"[ ! -f {root}/apps/platform/.env ]" not in command
    # Both guards run before the cd they protect.
    assert command.index(f"[ ! -s {root}/apps/platform/.env ]") < command.index(
        f"cd {root} &&"
    )
    assert "HALF provisioned" in command


def test_private_coding_storage_deploy_is_default_off_and_relay_blind() -> None:
    """Coding storage credentials stay with Python, not the relay pool."""
    defaults_path = ROOT / "infra/ansible/roles/platform_app/defaults/main.yml"
    defaults = yaml.safe_load(defaults_path.read_text())
    template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    terraform = (
        ROOT / "infra/terraform/stacks/gcp-platform/coding-storage.tf"
    ).read_text()
    output_script = (ROOT / "infra/ansible/scripts/platform-app-env.sh").read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()

    assert defaults["platform_coding_catalog_enabled"] is False
    assert defaults["platform_coding_evidence_enabled"] is False
    assert defaults["platform_coding_storage_readiness_enabled"] is False
    assert defaults["platform_coding_catalog_endpoint"] == (
        "https://storage.googleapis.com"
    )
    assert defaults["platform_coding_evidence_endpoint"] == (
        "https://storage.googleapis.com"
    )
    assert "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY=" in template
    assert "platform_coding_catalog_access_key | quote" in template
    assert "platform_secrets.coding_catalog_secret_key | quote" in template
    assert "platform_coding_catalog_use_tls | bool" in template
    assert "DITTO_CODING_EVIDENCE_STORAGE_ACCESS_KEY=" in template
    assert "platform_coding_evidence_access_key | quote" in template
    assert "platform_secrets.coding_evidence_secret_key | quote" in template
    assert "platform_coding_evidence_use_tls | bool" in template
    assert "DITTO_CODING_STORAGE_READINESS_ENABLED=" in template
    assert "platform_coding_storage_readiness_enabled | bool" in template
    assert "DITTO_CODING_STORAGE_READINESS_ENVIRONMENT=" in template
    assert "platform_coding_catalog_enabled | bool" in tasks
    assert "platform_coding_evidence_enabled | bool" in tasks
    assert "platform_coding_storage_readiness_enabled | bool" in tasks
    assert "platform_env in ['dev', 'prod']" in tasks
    assert "platform_coding_catalog_bucket != platform_bucket" in tasks
    assert "platform_coding_catalog_bucket != (platform_hippius_bucket" in tasks
    assert "platform_coding_evidence_bucket != platform_bucket" in tasks
    assert "platform_coding_evidence_bucket != platform_coding_catalog_bucket" in tasks
    assert "platform_coding_catalog_access_key_read" not in tasks
    assert "coding_private_input_reader_platform" in terraform
    assert "coding_evidence_finalizer_platform" in terraform
    runtime_binding_tail = terraform.split(
        'resource "google_secret_manager_secret_iam_member" '
        '"coding_private_input_reader_platform"',
        1,
    )[1]
    runtime_binding_tail = runtime_binding_tail.split(
        'resource "google_project_iam_audit_config"', 1
    )[0]
    assert "coding_private_input_curator_hmac" not in runtime_binding_tail
    assert "PLATFORM_TARGET_ENV" in output_script
    for output in (
        "coding_private_input_bucket_names",
        "coding_private_input_reader_hmac_access_ids",
        "coding_sealed_evidence_bucket_names",
        "coding_evidence_finalizer_hmac_access_ids",
    ):
        assert output in output_script
    for key in (
        "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY",
        "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY",
        "DITTO_CODING_EVIDENCE_STORAGE_ACCESS_KEY",
        "DITTO_CODING_EVIDENCE_STORAGE_SECRET_KEY",
    ):
        assert f'{key}: ""' in ecosystem
    assert 'DITTO_CODING_STORAGE_READINESS_ENABLED: "false"' in ecosystem
    assert 'DITTO_CODING_STORAGE_READINESS_ENVIRONMENT: "prod"' in ecosystem


def test_platform_app_env_selects_one_coding_storage_environment() -> None:
    command = r"""
terraform() {
  case "$4" in
    pg_internal_ip) printf '%s\n' '10.30.0.2' ;;
    storage_hmac_access_id) printf '%s\n' 'upload-access' ;;
    embedder_url|datapipeline_url) return 1 ;;
    coding_private_input_bucket_names)
      printf '%s\n' '{"dev":"input-dev","prod":"input-prod"}' ;;
    coding_private_input_reader_hmac_access_ids)
      printf '%s\n' '{"dev":"reader-dev","prod":"reader-prod"}' ;;
    coding_sealed_evidence_bucket_names)
      printf '%s\n' '{"dev":"evidence-dev","prod":"evidence-prod"}' ;;
    coding_evidence_finalizer_hmac_access_ids)
      printf '%s\n' '{"dev":"finalizer-dev","prod":"finalizer-prod"}' ;;
    *) return 1 ;;
  esac
}
export -f terraform
export GCP_OSLOGIN_USER=test PLATFORM_TARGET_ENV=prod
source infra/ansible/scripts/platform-app-env.sh >/dev/null
test "$PLATFORM_CODING_PRIVATE_INPUT_BUCKET" = input-prod
test "$PLATFORM_CODING_PRIVATE_INPUT_ACCESS_KEY" = reader-prod
test "$PLATFORM_CODING_EVIDENCE_BUCKET" = evidence-prod
test "$PLATFORM_CODING_EVIDENCE_ACCESS_KEY" = finalizer-prod
"""
    subprocess.run(
        ["bash", "-c", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_coding_storage_application_activation_is_dev_only() -> None:
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    dev = yaml.safe_load(
        (ROOT / "infra/ansible/host_vars/ditto-platform-dev.yml").read_text()
    )
    prod = yaml.safe_load(
        (ROOT / "infra/ansible/host_vars/ditto-platform-prod.yml").read_text()
    )
    compose = (ROOT / "docker-compose.yml").read_text()

    assert defaults["platform_coding_catalog_enabled"] is False
    assert defaults["platform_coding_evidence_enabled"] is False
    assert defaults["platform_coding_storage_readiness_enabled"] is False
    assert dev["platform_env"] == "dev"
    assert dev["platform_branch"] == "dev"
    assert dev["platform_coding_catalog_enabled"] is True
    assert dev["platform_coding_evidence_enabled"] is True
    assert dev["platform_coding_storage_readiness_enabled"] is True
    assert "platform_inference_relay_ports" not in dev
    assert prod["platform_env"] == "prod"
    assert prod["platform_branch"] == "main"
    assert prod["platform_coding_catalog_enabled"] is False
    assert prod["platform_coding_evidence_enabled"] is False
    assert prod["platform_coding_storage_readiness_enabled"] is False
    assert "DITTOBENCH_CODING_SHADOW_ENABLED:-false" in compose
    assert "DITTOBENCH_CODING_CANARY_ENABLED:-false" in compose
    assert "VALIDATOR_CODING_SHADOW_ENABLED:-false" in compose
