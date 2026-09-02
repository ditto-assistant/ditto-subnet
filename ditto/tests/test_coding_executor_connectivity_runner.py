from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/run-coding-executor-connectivity-canary.py"
SPEC = importlib.util.spec_from_file_location("coding_executor_canary_runner", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)

REVISION = "1" * 40
HOTKEY = "5" + "V" * 47
IMAGE = "ghcr.io/ditto-assistant/ditto-subnet-validator@sha256:" + "2" * 64
DESCRIPTOR = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "3" * 64
ORIGIN = "https://10.23.0.10:9443"


def _model(
    secret_root: str = "/var/lib/ditto-validator/coding-executor-mtls",
) -> dict[str, Any]:
    sources = {
        "coding-executor-validator-ca": "executor-ca.pem",
        "coding-executor-validator-client-cert": "validator-client.pem",
        "coding-executor-validator-client-key": "validator-client-key.pem",
    }
    return {
        "services": {
            "ditto-subnet": {
                "image": IMAGE,
                "pull_policy": "never",
                "environment": {
                    "VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED": "true",
                    "VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED": "false",
                    "VALIDATOR_CODING_SHADOW_ENABLED": "false",
                    "VALIDATOR_STACK_MODE": "managed",
                    "VALIDATOR_STACK_REVISION": REVISION,
                    "VALIDATOR_STACK_DESCRIPTOR_REF": DESCRIPTOR,
                    "VALIDATOR_HOTKEY": HOTKEY,
                    "VALIDATOR_CODING_EXECUTOR_BASE_URL": ORIGIN,
                    "VALIDATOR_CODING_EXECUTOR_TIMEOUT_SECONDS": "30",
                    **RUNNER.EXPECTED_RUNTIME_PATHS,
                },
                "secrets": [
                    {
                        "source": source,
                        "target": RUNNER.EXPECTED_SECRET_TARGETS[source],
                        "mode": "0400",
                    }
                    for source in sorted(sources)
                ],
            }
        },
        "secrets": {
            source: {"file": f"{secret_root}/{filename}"}
            for source, filename in sources.items()
        },
    }


def test_compose_model_requires_canary_only_digest_pinned_authority() -> None:
    plan = RUNNER.validate_compose_model(_model(), REVISION)
    assert plan.source_revision == REVISION
    assert plan.validator_image == IMAGE
    assert plan.descriptor_ref == DESCRIPTOR
    assert plan.validator_hotkey == HOTKEY
    assert plan.executor_origin_sha256 == hashlib.sha256(ORIGIN.encode()).hexdigest()
    assert len(plan.secret_paths) == 3


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("services", "ditto-subnet", "build"), {"context": "."}),
        (("services", "ditto-subnet", "image"), "validator:latest"),
        (
            (
                "services",
                "ditto-subnet",
                "environment",
                "VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED",
            ),
            "true",
        ),
        (
            (
                "services",
                "ditto-subnet",
                "environment",
                "VALIDATOR_CODING_SHADOW_ENABLED",
            ),
            "true",
        ),
        (
            (
                "services",
                "ditto-subnet",
                "environment",
                "VALIDATOR_CODING_EXECUTOR_BASE_URL",
            ),
            "https://8.8.8.8:9443",
        ),
        (("secrets", "coding-executor-validator-ca", "file"), "/dev/null"),
    ],
)
def test_compose_model_rejects_authority_drift(
    path: tuple[str, ...], value: object
) -> None:
    model = copy.deepcopy(_model())
    target: dict[str, Any] = model
    for element in path[:-1]:
        target = target[element]
    target[path[-1]] = value
    with pytest.raises(RUNNER.CanaryRunnerError):
        RUNNER.validate_compose_model(model, REVISION)


def test_receipt_is_diagnostic_canonical_and_private(tmp_path: Path) -> None:
    directory = tmp_path / "receipt"
    directory.mkdir(mode=0o700)
    plan = RUNNER.validate_compose_model(_model(), REVISION)
    started = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
    receipt = RUNNER.build_receipt(
        plan,
        started_at=started,
        completed_at=started + timedelta(seconds=2),
    )
    output = directory / "connectivity.json"
    RUNNER.write_receipt(output, receipt)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() == (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    assert receipt["authority"] == "operator-local-diagnostic"
    assert receipt["ticket_authority_used"] is False
    assert receipt["platform_contacted"] is False
    assert receipt["candidate_executed"] is False
    assert receipt["s3_accessed"] is False
    encoded = output.read_text()
    assert ORIGIN not in encoded
    assert "secret_paths" not in encoded


def test_receipt_and_credential_files_fail_closed(tmp_path: Path) -> None:
    credential_root = tmp_path / "credentials"
    credential_root.mkdir(mode=0o700)
    paths = []
    for name in ("ca.pem", "client.pem", "client-key.pem"):
        path = credential_root / name
        path.write_text("test")
        path.chmod(0o400)
        paths.append(str(path))
    RUNNER.validate_secret_files(tuple(paths))
    Path(paths[-1]).chmod(0o600)
    with pytest.raises(RUNNER.CanaryRunnerError):
        RUNNER.validate_secret_files(tuple(paths))

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    with pytest.raises(RUNNER.CanaryRunnerError):
        RUNNER.write_receipt(unsafe / "receipt.json", {})


def test_main_runs_only_managed_one_shot_service_and_commits_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "current"
    release.mkdir()
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(
        RUNNER,
        "_arguments",
        lambda: SimpleNamespace(
            release_dir=release,
            expected_revision=REVISION,
            evidence_out=output,
            executor_base_url=ORIGIN,
            timeout_seconds=120.0,
            transport_timeout_seconds=30.0,
        ),
    )
    monkeypatch.setattr(RUNNER, "_git_revision", lambda: REVISION)

    def resolve(
        command: list[str], **values: Any
    ) -> subprocess.CompletedProcess[bytes]:
        assert command[-3:] == ["config", "--format", "json"]
        environment = values["environment"]
        assert environment["VALIDATOR_CODING_EXECUTOR_CONNECTIVITY_CANARY_ENABLED"] == (
            "true"
        )
        assert environment["VALIDATOR_CODING_EXECUTOR_REMOTE_ENABLED"] == "false"
        assert environment["VALIDATOR_CODING_SHADOW_ENABLED"] == "false"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(_model()).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(RUNNER, "_run", resolve)
    monkeypatch.setattr(RUNNER, "validate_secret_files", lambda _: None)
    execution: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **values: Any) -> subprocess.CompletedProcess[bytes]:
        execution.append((command, values["env"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(RUNNER.subprocess, "run", run)
    receipts: list[dict[str, object]] = []

    def record_receipt(path: Path, receipt: dict[str, object]) -> None:
        assert path == output
        receipts.append(receipt)

    monkeypatch.setattr(RUNNER, "write_receipt", record_receipt)
    assert RUNNER.main() == 0
    assert len(execution) == 1
    command, environment = execution[0]
    assert command[-7:] == [
        "run",
        "--rm",
        "--no-deps",
        "--pull",
        "never",
        "-T",
        "ditto-subnet",
    ]
    assert "up" not in command
    assert environment["DITTO_ALLOW_MANAGED_STACK_MUTATION"] == "true"
    assert len(receipts) == 1 and receipts[0]["status"] == "passed"
