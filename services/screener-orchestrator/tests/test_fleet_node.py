from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from screener_capacity.controller import ControllerError
from screener_capacity.fleet_node import (
    _GUEST_OSINFO,
    _LIBVIRT_URI,
    _SERIAL_CAPTURE_LIMIT,
    ChannelSettings,
    FleetNode,
    KVMRunner,
    Settings,
    _build_failure_code,
    _build_script,
    _cloud_config,
    _load_credential,
    _runtime_script,
    _SerialCapture,
    _source_review_settings_environment,
)


def test_disposable_guest_os_profile_matches_verified_base_image() -> None:
    assert _GUEST_OSINFO == "debian12"


def test_disposable_guest_lifecycle_uses_one_system_libvirt_uri() -> None:
    assert _LIBVIRT_URI == "qemu:///system"


def test_serial_capture_is_service_owned_and_bounded() -> None:
    path = Path(f"/tmp/ditto-serial-test-{os.getpid()}.sock")
    capture = _SerialCapture(path)
    capture.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(b"x" * (_SERIAL_CAPTURE_LIMIT + 4_000) + b"MARKER")

    text = capture.finish()

    assert text.endswith("MARKER")
    assert len(text) == _SERIAL_CAPTURE_LIMIT
    assert not path.exists()


def test_kvm_runner_pins_system_uri_for_create_poll_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["qemu-img", "create"]:
            Path(command[-1]).touch()
        elif command[0] == "cloud-localds":
            Path(command[1]).touch()
        stdout = "shut off\n" if command[0] == "virsh" and "domstate" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    base = tmp_path / "base.qcow2"
    base.touch()
    runner = KVMRunner(
        base_image=base,
        jobs_root=Path("/tmp"),
        memory_mib=8192,
        vcpus=2,
        disk_gib=80,
    )

    ok, _console = runner.run(
        name=f"probe-{os.getpid()}", script="#!/bin/sh\ntrue\n", timeout_seconds=60
    )

    assert ok is True
    lifecycle = [call for call in calls if call[0] in {"virt-install", "virsh"}]
    assert lifecycle
    assert all(_LIBVIRT_URI in call for call in lifecycle)
    resize = next(call for call in calls if call[:2] == ["qemu-img", "resize"])
    assert resize[-1] == "80G"
    virt_install = next(call for call in calls if call[0] == "virt-install")
    seed = next(value for value in virt_install if "seed.iso" in value)
    assert "bus=virtio" in seed
    assert "readonly=on" in seed
    serial = next(value for value in virt_install if ".sock" in value)
    assert "mode=connect" in serial


def _settings(root: Path) -> Settings:
    return Settings(
        platform_url="https://platform.invalid",
        credential_file=root / "node.json",
        base_image=root / "base.qcow2",
        builder_image="registry.invalid/ditto/builder@sha256:" + "a" * 64,
        source_review_api_key_file=root / "review-key",
        jobs_root=root,
        source_review_env_file=None,
        interval_seconds=1,
        build_timeout_seconds=60,
        runtime_timeout_seconds=60,
        source_review_timeout_seconds=60,
        max_workers=4,
        vm_memory_mib=10240,
        vm_vcpus=8,
        vm_disk_gib=80,
        once=True,
    )


def test_source_review_job_receives_attempt_bound_policy() -> None:
    environment = _source_review_settings_environment(
        {
            "review_settings_revision": 36,
            "review_settings_checksum": "a" * 64,
            "review_settings": {
                "mode": "enforce",
                "l2_model": "moonshotai/kimi-k3",
                "l2_fallback_models": ["z-ai/glm-5.2", "openai/gpt-5.6-sol"],
                "l3_enabled": True,
                "l3_model": "openai/gpt-5.6-sol",
                "timeout_seconds": 900,
                "max_steps": 20,
                "source_review_max_steps": 200,
                "source_review_max_read_bytes": 8_000_000,
                "source_review_max_completion_tokens": 8_000,
                "source_review_reasoning_effort": "high",
                "source_review_model": "openai/gpt-5.6-luna",
                "source_review_timeout_seconds": 1_800,
                "concern_hold_count": 3,
                "clear_min_notes": 3,
                "adjudicator_mode": "enforce",
                "adjudicator_model": "z-ai/glm-5.3-flash",
                "adjudicator_max_steps": 24,
                "adjudicator_timeout_seconds": 600,
                "max_input_tokens": 1_000_000,
                "max_output_tokens": 128_000,
                "max_completion_tokens": 16_384,
                "max_cost_usd": 5,
                "critic_reasoning_effort": "medium",
                "cache_ttl_seconds": 604_800,
                "audit_retention_days": 30,
            },
        }
    )

    assert environment["SCREENER_L2_REVIEW_MODE"] == "enforce"
    assert environment["SCREENER_L3_REVIEW_ENABLED"] == "true"
    assert environment["SCREENER_ADJUDICATOR_MODE"] == "enforce"
    assert environment["SCREENER_SOURCE_REVIEW_MAX_STEPS"] == "200"
    assert environment["SCREENER_REVIEW_SETTINGS_REVISION"] == "36"
    assert environment["SCREENER_REVIEW_SETTINGS_CHECKSUM"] == "a" * 64


def test_node_credential_parser_ignores_signing_secret(tmp_path: Path) -> None:
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "environment": "prod",
                "node_id": "subnet-screener-1",
                "screener_hotkey": "5abc",
                "api_token": "t" * 48,
                "mnemonic": "must-not-leave-the-trusted-host",
            }
        )
    )

    credential = _load_credential(path)

    assert credential.node_id == "subnet-screener-1"
    assert not hasattr(credential, "mnemonic")


def test_build_seed_contains_only_the_attempt_token() -> None:
    token = "attempt-token-" + "x" * 48
    script = _build_script(
        platform_url="https://platform.invalid",
        build_id="12345678-1234-1234-1234-123456789abc",
        token=token,
        image="registry.invalid/ditto/builder@sha256:" + "a" * 64,
    )
    seed = _cloud_config(script)

    assert token not in seed
    assert "SCREENER_API_TOKEN" not in script
    assert "SCREENER_MNEMONIC" not in script
    assert base64.b64encode(token.encode()).decode() in script
    assert "DITTO_BUILD_EXIT_AFTER_COMPLETE=1" in script


def test_runtime_script_rejects_non_https_archive() -> None:
    encoded = base64.b64encode(b"http://metadata.invalid/secret").decode()

    with pytest.raises(ControllerError, match="invalid digest"):
        _runtime_script(archive_url_b64=encoded, expected_sha256="a" * 64)


def test_runtime_script_uses_only_non_secret_smoke_environment() -> None:
    script = _runtime_script(
        archive_url_b64=base64.b64encode(
            b"https://artifacts.invalid/image.tar"
        ).decode(),
        expected_sha256="a" * 64,
    )

    assert "OPENROUTER_API_KEY=sk-screener-smoke" in script
    assert "DITTOBENCH_DB=/tmp/dittobench.db" in script
    assert "SCREENER_SOURCE_REVIEW_API_KEY" not in script


def test_build_script_shell_quotes_platform_url() -> None:
    script = _build_script(
        platform_url="https://platform.invalid/path?name=value with-space",
        build_id="12345678-1234-1234-1234-123456789abc",
        token="attempt-token-" + "x" * 48,
        image="registry.invalid/ditto/builder@sha256:" + "a" * 64,
    )

    assert (
        "DITTO_PLATFORM_URL='https://platform.invalid/path?name=value with-space'"
        in script
    )


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("SOURCE", "FLEET_SUBMISSION_SOURCE_FAILED"),
        ("KANIKO", "FLEET_SUBMISSION_KANIKO_FAILED"),
        ("ARCHIVE", "FLEET_SUBMISSION_ARCHIVE_FAILED"),
        ("UPLOAD", "FLEET_SUBMISSION_UPLOAD_FAILED"),
        ("COMPLETE", "FLEET_SUBMISSION_COMPLETE_FAILED"),
        ("CONTRACT", "FLEET_SUBMISSION_CONTRACT_FAILED"),
    ],
)
def test_build_failure_code_preserves_safe_builder_stage(
    stage: str, expected: str
) -> None:
    console = f"private submitted output\nDITTO_SUBMISSION_BUILD_FAILED={stage}\r\n"

    assert _build_failure_code(ok=True, console=console) == expected


def test_build_failure_code_does_not_forward_unrecognized_console() -> None:
    assert (
        _build_failure_code(
            ok=True,
            console="DITTO_SUBMISSION_BUILD_FAILED=PRIVATE_SECRET\n",
        )
        == "FLEET_SUBMISSION_BUILD_VM_EXITED"
    )
    assert _build_failure_code(ok=False, console="token=private") == (
        "FLEET_SUBMISSION_BUILD_VM_FAILED"
    )

class _Control:
    def __init__(self) -> None:
        self.claimed: list[str] = []
        self.jobs = {
            "runtime": [{"build_id": "runtime"}],
            "build": [{"build_id": "build"}],
            "source_review": [],
        }

    def settings(self) -> ChannelSettings:
        return ChannelSettings(
            revision=1,
            screening_concurrency=8,
            sandbox_slots=2,
            build_concurrency=2,
            runtime_concurrency=2,
            source_review_concurrency=0,
        )

    def claim(self, lane: str) -> dict[str, str] | None:
        self.claimed.append(lane)
        jobs = self.jobs[lane]
        return jobs.pop(0) if jobs else None


def test_scheduler_finishes_runtime_before_admitting_build(tmp_path: Path) -> None:
    node = FleetNode(_settings(tmp_path))
    control = _Control()
    node.control = control  # type: ignore[assignment]
    node._run_runtime = lambda _job: None  # type: ignore[method-assign]
    node._run_build = lambda _job: None  # type: ignore[method-assign]

    try:
        assert node.tick() is True
    finally:
        node.executor.shutdown(wait=True)

    assert control.claimed.index("runtime") < control.claimed.index("build")


def test_source_review_uses_image_project_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class Control:
        def update(self, *_args: object, **_kwargs: object) -> None:
            pass

        def status(self, *_args: object) -> str:
            return "succeeded"

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    node = FleetNode(_settings(tmp_path))
    node.control = Control()  # type: ignore[assignment]
    monkeypatch.setattr(subprocess, "run", run)

    node._run_source_review(
        {
            "review_id": "12345678-1234-1234-1234-123456789abc",
            "attempt_id": "22345678-1234-1234-1234-123456789abc",
            "artifact_sha256": "a" * 64,
            "job_token": "job-token",
            "image_reference": "registry.invalid/ditto/screener@sha256:" + "b" * 64,
        }
    )

    assert commands[0][-3:] == [
        "/app/workers/screener/.venv/bin/python",
        "-m",
        "ditto_screener.source_review_job",
    ]
    user_index = commands[0].index("--user")
    assert commands[0][user_index + 1] == f"{os.getuid()}:{os.getgid()}"


def test_stop_request_prevents_new_claims(tmp_path: Path) -> None:
    node = FleetNode(_settings(tmp_path))
    node.control = pytest.fail  # type: ignore[assignment]
    node.request_stop()

    assert node.run() == 0
