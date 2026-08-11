"""Shared fixtures for the screener worker tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from ditto_screener.config import ScreenerConfig

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _default_config(**overrides: Any) -> ScreenerConfig:
    base: dict[str, Any] = {
        "platform_api_url": "http://platform.test",
        "api_token": "test-screener-token-at-least-32-characters",
        "screener_hotkey": _HOTKEY,
        "wallet_name": None,
        "wallet_hotkey": None,
        "screener_mnemonic": "x " * 11 + "x",
        "node_id": None,
        "node_provider": None,
        "node_provider_resource_id": None,
        "node_credential_file": None,
        "netuid": 3,
        "docker_bin": "docker",
        "docker_host": None,
        "require_rootless_docker": False,
        "build_timeout_seconds": 60.0,
        "run_timeout_seconds": 3.0,
        "image_build_memory": "8g",
        "gh_token_file": None,
        "pids_limit": 512,
        "health_path": "/health",
        "container_port": 8080,
        "smoke_env": (("OPENROUTER_API_KEY", "sk-screener-smoke"),),
        "max_tarball_bytes": 4 * 1024 * 1024,
        "poll_seconds": 0.01,
        "queue_limit": 20,
        "http_timeout_seconds": 5.0,
        "policy_manifest_file": None,
        "review_journal_file": None,
        "source_review_api_key_file": None,
        "source_review_model": "openai/gpt-5.6-luna",
        "source_review_base_url": "https://openrouter.ai/api/v1",
        "source_review_timeout_seconds": 180.0,
        "source_review_max_steps": 10,
        "static_preflight_v2_mode": "off",
        "static_preflight_audit_file": None,
        "l2_review_mode": "off",
        "l2_review_model": "moonshotai/kimi-k3",
        "l2_review_provider": "openrouter",
        "l2_fallback_models": ("z-ai/glm-5.2", "openai/gpt-5.6-sol"),
        "l3_review_enabled": True,
        "l3_review_model": "openai/gpt-5.6-sol",
        "l3_review_provider": "openrouter",
        "l2_analyzer_image": "ditto-screener-l2-analyzer:active",
        "l2_cache_dir": "/tmp/ditto-screener-test/l2/cache",
        "l2_audit_journal_file": "/tmp/ditto-screener-test/l2/audit.jsonl",
        "l2_timeout_seconds": 900.0,
        "l2_max_steps": 16,
        "l2_max_input_tokens": 425_000,
        "l2_max_output_tokens": 20_000,
        "l2_max_completion_tokens": 2_400,
        "l2_max_cost_usd": 2.0,
        "l2_analyst_reasoning_effort": "model_default",
        "l2_critic_reasoning_effort": "medium",
        "l2_cache_ttl_seconds": 7 * 86_400.0,
        "l2_audit_retention_days": 30,
        "review_settings_cache_file": "/tmp/ditto-screener-test/review-settings.json",
        "review_settings_max_stale_seconds": 900,
    }
    base.update(overrides)
    return ScreenerConfig(**base)


@pytest.fixture
def make_config(tmp_path: Any) -> Callable[..., ScreenerConfig]:
    """Factory: a valid :class:`ScreenerConfig` with per-test overrides."""

    def factory(**overrides: Any) -> ScreenerConfig:
        overrides.setdefault("l2_cache_dir", str(tmp_path / "l2" / "cache"))
        overrides.setdefault(
            "l2_audit_journal_file", str(tmp_path / "l2" / "audit.jsonl")
        )
        return _default_config(**overrides)

    return factory
