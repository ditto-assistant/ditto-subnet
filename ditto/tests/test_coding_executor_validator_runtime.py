"""Regression checks for default-off dedicated executor validator wiring."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/validator_stack"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = (ROLE / "tasks/main.yml").read_text()
ENVIRONMENT = (ROLE / "templates/validator.env.j2").read_text()
VERIFIER = (ROLE / "files/verify-coding-executor-client-identity.sh").read_text()
CANARY_VALIDATION = (ROLE / "tasks/validate_coding_canary.yml").read_text()
CANARY_RENDER_TEST = (
    ROOT / "infra/ansible/tests/validator-stack-coding-canary.yml"
).read_text()
INFRA_CI = (ROOT / ".github/workflows/infra-ci.yml").read_text()


def test_validator_executor_runtime_and_identity_are_independently_default_off() -> (
    None
):
    assert DEFAULTS["validator_stack_coding_shadow_enabled"] is False
    assert DEFAULTS["validator_stack_coding_executor_identity_enabled"] is False
    assert DEFAULTS["validator_stack_coding_executor_remote_enabled"] is False
    assert (
        DEFAULTS["validator_stack_coding_executor_connectivity_canary_enabled"] is False
    )
    assert (
        DEFAULTS["validator_stack_coding_executor_connectivity_canary_run_enabled"]
        is False
    )
    assert DEFAULTS["validator_stack_coding_executor_base_url"] == ""
    assert "validator_stack_coding_executor_identity_enabled | bool" in TASKS
    assert "validator_stack_coding_executor_remote_enabled | bool" in TASKS
    assert "validator_stack_coding_executor_connectivity_canary_enabled | bool" in (
        TASKS
    )
    assert "validator_stack_coding_shadow_enabled | bool" in TASKS
    assert "run-coding-executor-connectivity-canary.py" in TASKS
    assert "validator_stack_coding_executor_managed_release_directory" in TASKS


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


def test_validator_certification_canary_is_double_gated_default_off() -> None:
    assert DEFAULTS["validator_stack_dittobench_coding_canary_enabled"] is False
    assert DEFAULTS["validator_stack_coding_canary_enabled"] is False
    assert DEFAULTS["validator_stack_coding_canary_poll_seconds"] == 10
    assert DEFAULTS["validator_stack_coding_runtime_image_repository"] == ""
    assert DEFAULTS["validator_stack_coding_runtime_image_digest"] == ""
    # The dedicated rootless coding daemon and exact targets default to empty,
    # which refuses both canary switches and every lease.
    assert DEFAULTS["validator_stack_coding_docker_host"] == ""
    assert DEFAULTS["validator_stack_coding_canary_agent_ids"] == []
    assert DEFAULTS["validator_stack_coding_canary_validator_hotkey"] == ""
    # Validation runs before the first host mutation in the role.
    include = "ansible.builtin.include_tasks: validate_coding_canary.yml"
    assert TASKS.index(include) < TASKS.index("ansible.builtin.command")
    assert "validator_stack_dittobench_coding_canary_enabled | bool" in (
        CANARY_VALIDATION
    )
    assert "'^sha256:[0-9a-f]{64}\\Z'" in CANARY_VALIDATION
    assert "canary_docker_host is match('^unix:///[A-Za-z0-9._/-]+\\.sock\\Z')" in (
        CANARY_VALIDATION
    )
    assert (
        "validator_stack_coding_canary_validator_hotkey == validator_stack_hotkey"
        in CANARY_VALIDATION
    )
    for line in (
        "VALIDATOR_CODING_CANARY_ENABLED={{ 'true' if "
        "coding_canary_enabled else 'false' }}",
        "VALIDATOR_CODING_CANARY_AGENT_IDS={{ "
        "validator_stack_coding_canary_agent_ids | join(',') if "
        "coding_canary_enabled else '' }}",
        "VALIDATOR_CODING_CANARY_VALIDATOR_HOTKEY={{ "
        "validator_stack_coding_canary_validator_hotkey if "
        "coding_canary_enabled else '' }}",
        "DITTOBENCH_CODING_CANARY_ENABLED={{ 'true' if "
        "dittobench_coding_canary_enabled else 'false' }}",
        "DITTOBENCH_CODING_RUNTIME_IMAGE_DIGEST={{ "
        "validator_stack_coding_runtime_image_digest if "
        "dittobench_coding_canary_enabled else '' }}",
        "DITTOBENCH_CODING_DOCKER_HOST={{ "
        "validator_stack_coding_docker_host if "
        "dittobench_coding_canary_enabled else '' }}",
    ):
        assert line in ENVIRONMENT
    # Compose pins the image-baked pack; the host must not select another root.
    assert "\nDITTOBENCH_CODING_CERTIFICATION_ROOT=" not in ENVIRONMENT
    assert "tests/validator-stack-coding-canary.yml" in INFRA_CI
    assert "validator-stack-coding-canary-reject.yml" in CANARY_RENDER_TEST
