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


def test_validator_certification_canary_is_default_off_on_its_socket_route() -> None:
    assert DEFAULTS["validator_stack_coding_canary_enabled"] is False
    assert DEFAULTS["validator_stack_coding_canary_poll_seconds"] == 10
    # Exact targets and the socket route default to empty, which refuses the
    # validator switch and every lease.
    assert DEFAULTS["validator_stack_coding_canary_targets"] == []
    assert DEFAULTS["validator_stack_coding_canary_validator_hotkey"] == ""
    assert DEFAULTS["validator_stack_coding_certification_socket_uid"] == 0
    assert DEFAULTS["validator_stack_coding_certification_socket_gid"] == 0
    assert DEFAULTS["validator_stack_coding_certification_control_token_path"] == ""
    assert DEFAULTS["validator_stack_coding_certification_runtime_image_digest"] == ""
    assert DEFAULTS["validator_stack_coding_certification_pack_manifest_sha256"] == ""
    # The Compose scorer's certification route and its switch are retired.
    for retired in (
        "validator_stack_dittobench_coding_canary_enabled",
        "validator_stack_coding_runtime_image_repository",
        "validator_stack_coding_runtime_image_digest",
        "validator_stack_coding_docker_host",
        "validator_stack_coding_egress_network",
        "validator_stack_coding_egress_proxy",
        "validator_stack_coding_host_gateway_ip",
    ):
        assert retired not in DEFAULTS
        assert retired not in ENVIRONMENT
        assert retired not in CANARY_VALIDATION
    assert "DITTOBENCH_CODING_" not in ENVIRONMENT
    # Validation runs before the first host mutation in the role.
    include = "ansible.builtin.include_tasks: validate_coding_canary.yml"
    assert TASKS.index(include) < TASKS.index("ansible.builtin.command")
    assert (
        "validator_stack_coding_canary_validator_hotkey == validator_stack_hotkey"
        in CANARY_VALIDATION
    )
    # The unconditional refusal is lifted: every assertion is gated on the
    # switch, and the socket route and bearer path are required.
    assert "\n      - not canary_validator\n" not in CANARY_VALIDATION
    for required in (
        "validator_stack_coding_certification_socket_uid",
        "validator_stack_coding_certification_socket_gid",
        "validator_stack_coding_certification_control_token_path",
        "validator_stack_coding_certification_runtime_image_digest",
        "validator_stack_coding_certification_pack_manifest_sha256",
    ):
        assert required in CANARY_VALIDATION
    for line in (
        "VALIDATOR_CODING_CANARY_ENABLED={{ 'true' if "
        "coding_canary_enabled else 'false' }}",
        "VALIDATOR_CODING_CANARY_TARGETS={{ "
        "(validator_stack_coding_canary_targets | map(attribute='agent_id') | "
        "zip(validator_stack_coding_canary_targets | "
        "map(attribute='artifact_sha256'), "
        "validator_stack_coding_canary_targets | "
        "map(attribute='screened_image_sha256')) | map('join', ':') | "
        "join(',')) if coding_canary_enabled else '' }}",
        "VALIDATOR_CODING_CANARY_VALIDATOR_HOTKEY={{ "
        "validator_stack_coding_canary_validator_hotkey if "
        "coding_canary_enabled else '' }}",
        # The socket's parent directory, never the socket file; off, /dev/null.
        "VALIDATOR_CODING_CERTIFICATION_SOCKET_HOST_DIRECTORY={{ "
        "'/run/ditto-coding-certification' if coding_canary_enabled "
        "else '/dev/null' }}",
        "VALIDATOR_CODING_CERTIFICATION_CONTROL_TOKEN_HOST_PATH={{ "
        "validator_stack_coding_certification_control_token_path if "
        "coding_canary_enabled else '/dev/null' }}",
        "VALIDATOR_CODING_CERTIFICATION_SOCKET_UID={{ "
        "validator_stack_coding_certification_socket_uid if "
        "coding_canary_enabled else '' }}",
        "VALIDATOR_CODING_CERTIFICATION_SOCKET_GID={{ "
        "validator_stack_coding_certification_socket_gid if "
        "coding_canary_enabled else '' }}",
        "VALIDATOR_CODING_CERTIFICATION_RUNTIME_IMAGE_DIGEST={{ "
        "validator_stack_coding_certification_runtime_image_digest if "
        "coding_canary_enabled else '' }}",
        "VALIDATOR_CODING_CERTIFICATION_PACK_MANIFEST_SHA256={{ "
        "validator_stack_coding_certification_pack_manifest_sha256 if "
        "coding_canary_enabled else '' }}",
    ):
        assert line in ENVIRONMENT
    # The bearer is only ever a mounted file: never rendered as a value.
    assert "VALIDATOR_CODING_CERTIFICATION_CONTROL_TOKEN=" not in ENVIRONMENT
    assert "control.sock" not in ENVIRONMENT
    assert "tests/validator-stack-coding-canary.yml" in INFRA_CI
    assert "validator-stack-coding-canary-reject.yml" in CANARY_RENDER_TEST
