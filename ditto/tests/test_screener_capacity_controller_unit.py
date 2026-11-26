"""The controller unit renders a well-formed ExecStart under Ansible templating."""

from __future__ import annotations

from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2")

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "infra/ansible/roles/screener_capacity_controller"
TEMPLATE = ROLE / "templates/ditto-screener-capacity.service.j2"

BASE = {
    "screener_capacity_owner": "deploy",
    "screener_capacity_group": "ditto",
    "screener_capacity_service_dir": "/opt/ditto-subnet/services/screener-orchestrator",
    "screener_capacity_state_dir": "/var/lib/ditto-screener-capacity",
    "screener_capacity_venv": "/opt/ditto-subnet/services/screener-orchestrator/.venv",
    "screener_capacity_platform_url": "https://platform-api.heyditto.ai",
    "screener_capacity_config_dir": "/etc/ditto-screener-capacity",
    "screener_capacity_environment": "prod",
    "screener_capacity_global_cap": 6,
    "screener_capacity_jobs_per_slot": 6,
    "screener_capacity_interval_seconds": 30,
    "screener_capacity_gce_project": "ditto-app-dev",
    "screener_capacity_gce_region": "us-central1",
    "screener_capacity_gce_mig": "ditto-screener-fleet",
    "screener_capacity_aws_region": "us-east-1",
    "screener_capacity_aws_web_identity_audience": "ditto-screener-capacity",
    "screener_capacity_primary_wake_grace_seconds": 600,
}


def _render(**overrides: object) -> list[str]:
    # Ansible's template module defaults: trim_blocks on, lstrip_blocks off.
    environment = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True)
    template = environment.from_string(TEMPLATE.read_text())
    return template.render(**{**BASE, **overrides}).splitlines()


def _exec_start(lines: list[str]) -> list[str]:
    start = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
    command = [lines[start]]
    while command[-1].endswith("\\"):
        command.append(lines[start + len(command)])
    return command


@pytest.mark.parametrize(
    ("instance", "role", "aws"),
    [
        ("", "", False),
        ("i-0123456789abcdef0", "", False),
        (
            "i-0123456789abcdef0",
            "arn:aws:iam::339713011188:role/ditto-screener-capacity-wake",
            True,
        ),
    ],
)
def test_exec_start_ends_before_restart(instance: str, role: str, aws: bool) -> None:
    lines = _render(
        screener_capacity_aws_primary_instance_id=instance,
        screener_capacity_aws_primary_role_arn=role,
    )
    command = _exec_start(lines)

    assert "Restart=always" in lines
    assert not any("Restart=" in line for line in command)
    assert sum("--gce-mig ditto-screener-fleet" in line for line in command) == 1
    assert any("--aws-primary-instance-id" in line for line in command) is aws
    if aws:
        assert command[-1].strip() == "--primary-wake-grace-seconds 600"
    else:
        assert command[-1].strip() == "--gce-mig ditto-screener-fleet"
