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


def test_private_coding_catalog_deploy_is_default_off_and_relay_blind() -> None:
    """Catalog credentials stay with the Python API, not the shared relay pool."""
    defaults_path = ROOT / "infra/ansible/roles/platform_app/defaults/main.yml"
    defaults = yaml.safe_load(defaults_path.read_text())
    template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    terraform = (ROOT / "infra/terraform/stacks/gcp-platform/main.tf").read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()

    assert defaults["platform_coding_catalog_enabled"] is False
    assert "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY=" in template
    assert "platform_coding_catalog_use_tls | bool" in template
    assert "platform_coding_catalog_enabled | bool" in tasks
    assert "platform_coding_catalog_bucket != platform_bucket" in tasks
    assert "platform_coding_catalog_bucket != (platform_hippius_bucket" in tasks
    assert "secret_coding_catalog_access_key != secret_hippius_access_key_id" in tasks
    assert (
        "platform_coding_catalog_access_key_read.stdout | default('') | length > 0"
        in tasks
    )
    assert (
        "platform_coding_catalog_access_key_read.stdout != "
        "(platform_secrets.hippius_access_key_id | default(''))" in tasks
    )
    for secret in (
        "platform-coding-catalog-access-key",
        "platform-coding-catalog-secret-key",
    ):
        assert secret in terraform
    for key in (
        "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY",
        "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY",
    ):
        assert f'{key}: ""' in ecosystem
