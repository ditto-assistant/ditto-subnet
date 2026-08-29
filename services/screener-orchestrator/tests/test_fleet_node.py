from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from screener_capacity.controller import ControllerError
from screener_capacity.fleet_node import (
    ChannelSettings,
    FleetNode,
    Settings,
    _build_script,
    _cloud_config,
    _load_credential,
    _runtime_script,
)


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
