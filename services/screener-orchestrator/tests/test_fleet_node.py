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
    _build_script,
    _cloud_config,
    _load_credential,
    _runtime_script,
    _SerialCapture,
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


def test_stop_request_prevents_new_claims(tmp_path: Path) -> None:
    node = FleetNode(_settings(tmp_path))
    node.control = pytest.fail  # type: ignore[assignment]
    node.request_stop()

    assert node.run() == 0
