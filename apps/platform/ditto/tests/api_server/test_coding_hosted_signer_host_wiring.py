"""Ansible-rendered hosted signer settings parse and load in the Platform loader.

infra/ansible/tests/coding-hosted-control-signer.yml proves with real Ansible that
platform.env.j2 renders exactly these lines, and the root suite
ditto/tests/test_coding_hosted_control_signer_wiring.py owns every infra-only
assertion. This module keeps only the cross-checks that need the Platform
loader; the two infra files it reads are Platform CI trigger paths. The seed
below is the public synthetic test key, never production configuration.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import replace
from pathlib import Path

import bittensor
import pytest

from ditto.api_server.coding_hosted_signer import load_hosted_control_signer
from ditto.api_server.coding_hosted_signer_config import (
    HostedControlSignerConfig,
    check_hosted_signer_config,
    parse_hosted_signer_config_from_env,
)

PLATFORM_ROOT = Path(__file__).parents[3]
MONOREPO_ROOT = PLATFORM_ROOT.parents[1]
ROLE = MONOREPO_ROOT / "infra" / "ansible" / "roles" / "platform_app"
# Read below; both are listed in .github/workflows/platform-ci.yml paths.
TEMPLATE = ROLE / "templates" / "platform.env.j2"
SIGNER_GUARD = ROLE / "tasks" / "coding_hosted_signer.yml"
FIXED_SEED = Path("/etc/ditto-platform/coding-hosted-signer/seed")
SEED = bytes.fromhex("11" * 32)  # Public synthetic test key, never production config.
KEY = bittensor.Keypair.create_from_seed(SEED.hex())
ENV_KEYS = (
    "DITTO_CODING_HOSTED_CONTROL_ENABLED",
    "DITTO_CODING_HOSTED_SIGNER_SEED_FILE",
    "DITTO_CODING_HOSTED_SIGNER_HOTKEY",
)
HOTKEY_VALUE = "{{ platform_coding_hosted_signer_hotkey | quote }}"


def _rendered_environment(*, enabled: bool, hotkey: str) -> dict[str, str]:
    """Parse one template branch the way `set -a; . ./.env` would."""
    template = TEMPLATE.read_text()
    block = template[
        template.index("{% if platform_coding_hosted_control_enabled | bool %}") :
    ]
    block = block[: block.index("{% endif %}")]
    enabled_branch, disabled_branch = block.split("{% else %}")
    environment: dict[str, str] = {}
    for line in (enabled_branch if enabled else disabled_branch).splitlines():
        if line.startswith(("{%", "#")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.replace(HOTKEY_VALUE, shlex.quote(hotkey))
        assert "{{" not in value and "{%" not in value
        (environment[key],) = shlex.split(value)
    return environment


def _apply(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


def test_rendered_disabled_environment_never_reads_a_seed(monkeypatch):
    environment = _rendered_environment(enabled=False, hotkey=KEY.ss58_address)
    assert environment == {"DITTO_CODING_HOSTED_CONTROL_ENABLED": "false"}
    _apply(monkeypatch, environment)
    monkeypatch.setattr(
        "ditto.api_server.coding_hosted_signer.read_private",
        lambda *_args: pytest.fail("disabled rendering read the seed"),
    )

    config = parse_hosted_signer_config_from_env()

    assert config == HostedControlSignerConfig()
    assert load_hosted_control_signer(config, process_role="platform") is None


def test_rendered_enabled_environment_names_the_fixed_seed_and_binds_the_key(
    monkeypatch, tmp_path
):
    environment = _rendered_environment(enabled=True, hotkey=KEY.ss58_address)
    assert environment == {
        "DITTO_CODING_HOSTED_CONTROL_ENABLED": "true",
        "DITTO_CODING_HOSTED_SIGNER_SEED_FILE": str(FIXED_SEED),
        "DITTO_CODING_HOSTED_SIGNER_HOTKEY": KEY.ss58_address,
    }
    _apply(monkeypatch, environment)

    config = parse_hosted_signer_config_from_env()
    check_hosted_signer_config(config)

    assert config.enabled is True
    assert config.seed_file == FIXED_SEED
    assert config.expected_hotkey == KEY.ss58_address

    # The fixed path belongs to the protected host. Prove the same parsed
    # identity loads from an equivalent owner-only synthetic placement.
    directory = tmp_path / "coding-hosted-signer"
    directory.mkdir(mode=0o700)
    seed = directory / "seed"
    seed.write_bytes(SEED)
    seed.chmod(0o600)
    signer = load_hosted_control_signer(
        replace(config, seed_file=seed), process_role="platform"
    )
    assert signer is not None
    assert signer.ss58_address == KEY.ss58_address
    signer.close()
    assert load_hosted_control_signer(config, process_role="relay") is None


def test_ansible_hotkey_guard_is_no_wider_than_the_platform_loader():
    guard = SIGNER_GUARD.read_text()
    (pattern,) = re.findall(
        r"platform_coding_hosted_signer_hotkey is match\('([^']+)'\)", guard
    )
    assert "platform_coding_hosted_signer_hotkey | length == 48" in guard
    assert re.fullmatch(pattern.strip("^$"), KEY.ss58_address)

    # Every Ansible-accepted value is also accepted by the loader's own check.
    check_hosted_signer_config(
        HostedControlSignerConfig(
            True,
            FIXED_SEED,
            "5" + "1" * 47,
        )
    )
