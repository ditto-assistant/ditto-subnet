"""Regression checks for the default-off attestation-bound scorer service."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_executor"
DEFAULTS = (ROLE / "defaults/main.yml").read_text()
TASKS = (ROLE / "tasks/main.yml").read_text()
UNIT = ROLE / "templates/ditto-coding-executor-scorer.service.j2"
VERIFY_PATH = ROLE / "files/verify-scorer-service.py"
RUNNER_PATH = ROLE / "files/run-scorer-service.py"
SPEC = importlib.util.spec_from_file_location("verify_scorer_service", VERIFY_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)


def test_scorer_service_is_default_off_and_credential_bound() -> None:
    unit = UNIT.read_text()
    assert "coding_executor_scorer_service_enabled: false" in DEFAULTS
    assert "when: coding_executor_scorer_service_enabled | bool" in TASKS
    for dependency in (
        "coding_executor_scorer_runtime_materialize_enabled | bool",
        "coding_executor_capability_egress_enabled | bool",
        "coding_executor_capability_ingress_enabled | bool",
        "coding_executor_client_guard_enabled | bool",
    ):
        assert dependency in TASKS
    assert "LoadCredential=control-token:" in unit
    assert "CREDENTIALS_DIRECTORY" in RUNNER_PATH.read_text()
    assert "EnvironmentFile=" not in unit
    assert "AF_UNIX AF_INET" in unit
    assert "verify-scorer-service.py" in unit
    assert "coding-executor-client-guard.py --once" in unit


def test_scorer_service_derives_only_a_digest_pinned_runtime_repository() -> None:
    assert (
        VERIFY.runtime_repository(
            {"image_reference": "registry.invalid/runtime@sha256:" + "1" * 64}
        )
        == "registry.invalid/runtime"
    )

    with pytest.raises(VERIFY.VerificationError, match="reference is invalid"):
        VERIFY.runtime_repository(
            {"image_reference": "registry.invalid/runtime:latest"}
        )
