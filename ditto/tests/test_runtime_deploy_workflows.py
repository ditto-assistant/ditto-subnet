import os
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


def test_platform_deploy_passes_config_over_ssh_stdin(tmp_path: Path) -> None:
    """The deploy secret must reach the VM without entering gcloud's argv/env."""
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    assert step["env"]["DITTO_UPLOAD_PAYMENT_ADDRESS"] == (
        "${{ secrets.DITTO_UPLOAD_PAYMENT_ADDRESS }}"
    )
    assert "${{ secrets.DITTO_UPLOAD_PAYMENT_ADDRESS }}" not in step["run"]

    # A stub receives exactly the gcloud process boundary: the command line,
    # environment and stdin. No live SSH connection or secret is involved.
    stub = tmp_path / "gcloud"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "assert 'DITTO_UPLOAD_PAYMENT_ADDRESS' not in os.environ\n"
        "assert 'DITTO_DASHBOARD_WANDB_URL' not in os.environ\n"
        "assert 'SUBTENSOR_ARCHIVE_RPC_URL' not in os.environ\n"
        "assert 'SUBTENSOR_ARCHIVE_RPC_AUTH_MODE' not in os.environ\n"
        "args = ' '.join(sys.argv[1:])\n"
        "assert 'payment-address-sentinel' not in args\n"
        "assert 'wandb-url-sentinel' not in args\n"
        "assert '--ssh-flag=-T' in args\n"
        "assert 'IFS= read -r DITTO_UPLOAD_PAYMENT_ADDRESS' in args\n"
        "assert 'IFS= read -r DITTO_DASHBOARD_WANDB_URL' in args\n"
        "assert 'IFS= read -r SUBTENSOR_ARCHIVE_RPC_URL' in args\n"
        "assert 'IFS= read -r SUBTENSOR_ARCHIVE_RPC_AUTH_MODE' in args\n"
        "assert sys.stdin.read() == 'payment-address-sentinel\\n'"
        " + 'wandb-url-sentinel\\n' + 'wss://archive-sentinel\\n'"
        " + 'query\\n'\n"
        "print('deployed-commit=' + os.environ['DEPLOY_REVISION'])\n"
    )
    stub.chmod(0o700)
    deploy_log = tmp_path / "deploy.log"
    script = (
        step["run"]
        .replace("${{ steps.target.outputs.vm }}", "test-vm")
        .replace("/tmp/deploy.log", str(deploy_log))
    )
    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DITTO_UPLOAD_PAYMENT_ADDRESS": "payment-address-sentinel",
        "DITTO_DASHBOARD_WANDB_URL": "wandb-url-sentinel",
        "SUBTENSOR_ARCHIVE_RPC_URL": "wss://archive-sentinel",
        "SUBTENSOR_ARCHIVE_RPC_AUTH_MODE": "query",
        "DEPLOY_REVISION": "a" * 40,
        "GCP_ZONE": "test-zone",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"commit={'a' * 40}\n"
    assert "payment-address-sentinel" not in deploy_log.read_text()


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
