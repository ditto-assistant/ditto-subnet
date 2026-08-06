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
