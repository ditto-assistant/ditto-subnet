"""Shared fixtures for the screener worker tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ditto.screener.config import ScreenerConfig

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _default_config(**overrides: Any) -> ScreenerConfig:
    base: dict[str, Any] = {
        "platform_api_url": "http://platform.test",
        "screener_hotkey": _HOTKEY,
        "wallet_name": None,
        "wallet_hotkey": None,
        "screener_mnemonic": "x " * 11 + "x",
        "netuid": 3,
        "docker_bin": "docker",
        "build_timeout_seconds": 60.0,
        "run_timeout_seconds": 3.0,
        "build_memory": "2g",
        "gh_token_file": None,
        "pids_limit": 512,
        "health_path": "/health",
        "container_port": 8080,
        "max_tarball_bytes": 4 * 1024 * 1024,
        "poll_seconds": 0.01,
        "queue_limit": 20,
        "http_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return ScreenerConfig(**base)


@pytest.fixture
def make_config() -> Callable[..., ScreenerConfig]:
    """Factory: a valid :class:`ScreenerConfig` with per-test overrides."""
    return _default_config
