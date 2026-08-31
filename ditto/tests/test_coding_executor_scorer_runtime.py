"""Security checks for materializing the dormant scorer host runtime."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_executor"
DEFAULTS = (ROLE / "defaults/main.yml").read_text()
TASKS = (ROLE / "tasks/main.yml").read_text()
MATERIALIZER_PATH = ROLE / "files/materialize-scorer-runtime.py"
MATERIALIZER_TEXT = MATERIALIZER_PATH.read_text().lower()
SPEC = importlib.util.spec_from_file_location(
    "scorer_runtime_materializer", MATERIALIZER_PATH
)
assert SPEC is not None and SPEC.loader is not None
MATERIALIZER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MATERIALIZER
SPEC.loader.exec_module(MATERIALIZER)


def _attestation() -> dict[str, str]:
    return {
        "archive_sha256": "1" * 64,
        "bundle_manifest_sha256": "2" * 64,
        "image_id": "sha256:" + "3" * 64,
        "image_reference": (
            "ghcr.io/ditto-assistant/dittobench-coding-executor-scorer@sha256:"
            + "4" * 64
        ),
        "locked_policy_sha256": "5" * 64,
        "platform": "linux/amd64",
        "release_manifest_sha256": "6" * 64,
        "schema": "dittobench-coding-executor-scorer-image-attestation-v1",
        "scorer_contract": "1",
        "source_revision": "a" * 40,
    }


def _image(attestation: dict[str, str]) -> dict[str, Any]:
    return {
        "Architecture": "amd64",
        "Config": {
            "Cmd": None,
            "Entrypoint": ["/dittobench-coding-executor-scorer"],
            "Env": ["PATH=/"],
            "ExposedPorts": None,
            "Healthcheck": None,
            "Labels": {
                "io.heyditto.dittobench.coding-executor-scorer-contract": "1",
                "io.heyditto.dittobench.coding-executor-locked-policy-sha256": (
                    attestation["locked_policy_sha256"]
                ),
                "org.opencontainers.image.revision": attestation["source_revision"],
            },
            "User": "65532:65532",
            "Volumes": None,
        },
        "Id": attestation["image_id"],
        "Os": "linux",
    }


def test_scorer_runtime_materialization_is_default_off_and_nonserving() -> None:
    assert "coding_executor_scorer_runtime_materialize_enabled: false" in DEFAULTS
    assert "when: coding_executor_scorer_runtime_materialize_enabled | bool" in TASKS
    assert (
        "not (coding_executor_scorer_runtime_materialize_enabled | bool) or "
        "coding_executor_scorer_image_load_enabled | bool" in TASKS
    )
    assert "materialize-scorer-runtime.py" in TASKS
    assert '["create", "--network", "none"' in MATERIALIZER_TEXT
    for command in ("container start", "container run", "image pull"):
        assert command not in MATERIALIZER_TEXT
    materialize_block = TASKS.split(
        "Materialize the attested scorer runtime without starting it", 1
    )[1].split("Verify a protected staged coding runtime bundle", 1)[0]
    assert "systemd_service" not in materialize_block


def test_scorer_runtime_materializer_binds_all_output_to_the_input_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _attestation()
    recorded: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(MATERIALIZER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        MATERIALIZER,
        "read_scorer_attestation",
        lambda _path: (attestation, "7" * 64),
    )
    monkeypatch.setattr(MATERIALIZER, "inspect_daemon", lambda: None)
    monkeypatch.setattr(MATERIALIZER, "inspect_scorer_image", lambda _value: None)
    monkeypatch.setattr(
        MATERIALIZER,
        "copy_attested_runtime",
        lambda _value: (True, False, "8" * 64, MATERIALIZER.LOCKED_POLICY_FILE_SHA256),
    )
    monkeypatch.setattr(
        MATERIALIZER,
        "write_runtime_attestation",
        lambda value: recorded.setdefault("value", value) is not None,
    )

    assert MATERIALIZER.materialize_scorer_runtime() is True
    assert recorded["value"] == {
        "binary_sha256": "8" * 64,
        "locked_policy_sha256": attestation["locked_policy_sha256"],
        "policy_file_sha256": MATERIALIZER.LOCKED_POLICY_FILE_SHA256,
        "schema": "dittobench-coding-executor-scorer-runtime-v1",
        "scorer_attestation_sha256": "7" * 64,
        "scorer_contract": "1",
        "scorer_image_id": attestation["image_id"],
        "scorer_image_reference": attestation["image_reference"],
        "source_revision": attestation["source_revision"],
    }


def test_scorer_runtime_copy_never_starts_the_temporary_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation_directory = tmp_path / "attestations"
    attestation_directory.mkdir()
    runtime_binary = tmp_path / "runtime" / "scorer"
    runtime_binary.parent.mkdir()
    runtime_policy = tmp_path / "policy" / "locked.json"
    runtime_policy.parent.mkdir()
    monkeypatch.setattr(
        MATERIALIZER,
        "EXPECTED_RUNTIME_ATTESTATION_PATH",
        attestation_directory / "runtime.json",
    )
    monkeypatch.setattr(MATERIALIZER, "EXPECTED_RUNTIME_BINARY_PATH", runtime_binary)
    monkeypatch.setattr(MATERIALIZER, "EXPECTED_RUNTIME_POLICY_PATH", runtime_policy)
    commands: list[list[str]] = []

    def fake_docker_output(arguments: list[str], *, timeout: int) -> bytes:
        assert timeout > 0
        commands.append(arguments)
        if arguments[:2] == ["create", "--network"]:
            return ("a" * 64).encode()
        if arguments[0] == "cp":
            destination = Path(arguments[-1])
            if arguments[1].endswith(MATERIALIZER.SCORER_IMAGE_BINARY_PATH):
                (destination / MATERIALIZER.SCORER_BINARY_NAME).write_bytes(b"binary")
            else:
                (destination / runtime_policy.name).write_bytes(b"policy")
            return b""
        assert arguments == ["rm", "-f", "a" * 64]
        return b""

    monkeypatch.setattr(MATERIALIZER, "docker_output", fake_docker_output)
    monkeypatch.setattr(MATERIALIZER, "validate_binary", lambda _path: "b" * 64)
    monkeypatch.setattr(
        MATERIALIZER,
        "validate_policy",
        lambda _path: MATERIALIZER.LOCKED_POLICY_FILE_SHA256,
    )
    installed: list[Path] = []

    def fake_install(_source: Path, destination: Path, **_kwargs: Any) -> bool:
        installed.append(destination)
        return True

    monkeypatch.setattr(MATERIALIZER, "install_file", fake_install)

    result = MATERIALIZER.copy_attested_runtime(_attestation())

    assert result == (True, True, "b" * 64, MATERIALIZER.LOCKED_POLICY_FILE_SHA256)
    assert installed == [runtime_binary, runtime_policy]
    assert commands[0] == ["create", "--network", "none", "sha256:" + "3" * 64]
    assert commands[1][:2] == [
        "cp",
        "a" * 64 + ":/dittobench-coding-executor-scorer",
    ]
    assert commands[2][:2] == [
        "cp",
        "a" * 64 + ":/opt/ditto/coding/coding_inference_policy_locked_v1.json",
    ]
    assert commands[3] == ["rm", "-f", "a" * 64]
    assert all("start" not in command and "run" not in command for command in commands)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda image: image["Config"]["Labels"].__setitem__(
                "io.heyditto.dittobench.coding-executor-locked-policy-sha256",
                "0" * 64,
            ),
            "locked policy",
        ),
        (
            lambda image: image["Config"].__setitem__("User", "0"),
            "user is invalid",
        ),
        (
            lambda image: image["Config"].__setitem__(
                "ExposedPorts", {"11438/tcp": {}}
            ),
            "volume or exposed port",
        ),
    ],
)
def test_scorer_runtime_materializer_rejects_image_drift(
    mutate: Any,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _attestation()
    image = _image(attestation)
    mutate(image)
    monkeypatch.setattr(
        MATERIALIZER,
        "docker_output",
        lambda _arguments, **_kwargs: json.dumps(image).encode(),
    )

    with pytest.raises(MATERIALIZER.MaterializationError, match=error):
        MATERIALIZER.inspect_scorer_image(attestation)
