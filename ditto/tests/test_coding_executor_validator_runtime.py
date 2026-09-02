"""Regression checks for default-off dedicated executor validator wiring."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/validator_stack"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = (ROLE / "tasks/main.yml").read_text()
ENVIRONMENT = (ROLE / "templates/validator.env.j2").read_text()
VERIFIER = (ROLE / "files/verify-coding-executor-client-identity.sh").read_text()


def test_validator_executor_runtime_and_identity_are_independently_default_off() -> (
    None
):
    assert DEFAULTS["validator_stack_coding_shadow_enabled"] is False
    assert DEFAULTS["validator_stack_coding_executor_identity_enabled"] is False
    assert DEFAULTS["validator_stack_coding_executor_remote_enabled"] is False
    assert (
        DEFAULTS["validator_stack_coding_executor_connectivity_canary_enabled"] is False
    )
    assert DEFAULTS["validator_stack_coding_executor_base_url"] == ""
    assert "validator_stack_coding_executor_identity_enabled | bool" in TASKS
    assert "validator_stack_coding_executor_remote_enabled | bool" in TASKS
    assert "validator_stack_coding_executor_connectivity_canary_enabled | bool" in (
        TASKS
    )
    assert "validator_stack_coding_shadow_enabled | bool" in TASKS


def test_validator_executor_identity_is_prepositioned_fixed_and_spiffe_bound() -> None:
    identity_root = "/var/lib/ditto-validator/coding-executor-mtls"
    assert DEFAULTS["validator_stack_coding_executor_identity_directory"] == (
        identity_root
    )
    assert "Ansible never copies their contents" in TASKS
    assert "item.stat.mode == '0400'" in TASKS
    assert "openssl verify -purpose sslclient" in VERIFIER
    assert "openssl pkey" in VERIFIER
    assert "spiffe://dittobench.ai/validator/$hotkey" in VERIFIER
    for forbidden in ("curl", "wget", "docker", "gcloud", "nc -l", "socat"):
        assert forbidden not in VERIFIER


def test_validator_environment_keeps_credentials_out_of_values() -> None:
    assert "VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED={{ 'true'" in ENVIRONMENT
    assert (
        "VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED={{ 'true'" in ENVIRONMENT
    )
    assert "VALIDATOR_CODING_EXECUTOR_BASE_URL={{" in ENVIRONMENT
    for filename in (
        "coding-executor-validator-ca.pem",
        "coding-executor-validator-client.pem",
        "coding-executor-validator-client-key.pem",
    ):
        assert f"/run/secrets/{filename}" in ENVIRONMENT
    assert "BEGIN CERTIFICATE" not in ENVIRONMENT
    assert "BEGIN PRIVATE KEY" not in ENVIRONMENT
