"""Tests for the screener env-driven config."""

from __future__ import annotations

import pytest

from ditto_screener.config import parse_screener_config_from_env
from ditto_screener.errors import ScreenerConfigError

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENER_HOTKEY", _HOTKEY)
    monkeypatch.setenv(
        "SCREENER_API_TOKEN", "test-screener-token-at-least-32-characters"
    )
    monkeypatch.setenv("SCREENER_MNEMONIC", _MNEMONIC)
    monkeypatch.setenv("SCREENER_SOURCE_REVIEW_API_KEY_FILE", "/run/secrets/luna")
    for k in (
        "SCREENER_WALLET_NAME",
        "SCREENER_WALLET_HOTKEY",
        "SCREENER_GH_TOKEN_FILE",
        "SCREENER_BUILD_TIMEOUT_SECONDS",
        "SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS",
        "SCREENER_REMOTE_BUILD_MODE",
        "SCREENER_BUILD_MEMORY",
        "SCREENER_IMAGE_BUILD_MEMORY",
        "NETUID",
    ):
        monkeypatch.delenv(k, raising=False)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    cfg = parse_screener_config_from_env()
    assert cfg.screener_hotkey == _HOTKEY
    assert cfg.api_token == "test-screener-token-at-least-32-characters"
    assert cfg.netuid == 118
    assert cfg.docker_bin == "docker"
    assert cfg.docker_host is None
    assert not cfg.require_rootless_docker
    assert cfg.container_port == 8080
    assert cfg.image_build_memory == "8g"
    assert cfg.remote_build_timeout_seconds == 1500
    assert cfg.remote_build_mode == "off"
    assert cfg.gh_token_file is None
    # Must default to (at least) the platform's 20 MiB upload cap, else the gate
    # false-fails legitimately-uploaded tarballs.
    assert cfg.max_tarball_bytes == 20 * 1024 * 1024
    # A dummy LLM key is injected by default so the reference harness (which
    # builds its OpenRouter Baseline before binding /health) boots during the
    # serve smoke.
    assert cfg.smoke_env == (("OPENROUTER_API_KEY", "sk-screener-smoke"),)
    assert cfg.signing_source_present()
    assert cfg.static_preflight_v2_mode == "off"
    assert cfg.static_preflight_audit_file is None
    assert cfg.l2_review_mode == "off"
    assert cfg.l2_review_model == "moonshotai/kimi-k3"
    assert cfg.l2_review_provider == "openrouter"
    assert cfg.l2_fallback_models == ("z-ai/glm-5.2", "openai/gpt-5.6-sol")
    assert cfg.l3_review_enabled is True
    assert cfg.l3_review_model == "openai/gpt-5.6-sol"
    assert cfg.l3_review_provider == "openrouter"
    assert cfg.l2_max_steps == 18
    assert cfg.l2_timeout_seconds == 900
    assert cfg.l2_max_input_tokens == 425_000
    assert cfg.l2_max_output_tokens == 20_000
    assert cfg.l2_max_cost_usd == 2.0
    assert cfg.l2_analyst_reasoning_effort == "model_default"
    assert cfg.l2_critic_reasoning_effort == "medium"


def test_image_build_memory_additively_replaces_legacy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_BUILD_MEMORY", "4g")
    assert parse_screener_config_from_env().image_build_memory == "4g"

    monkeypatch.setenv("SCREENER_IMAGE_BUILD_MEMORY", "8g")
    assert parse_screener_config_from_env().image_build_memory == "8g"


def test_remote_build_timeout_is_independent_and_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_BUILD_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS", "1800")

    cfg = parse_screener_config_from_env()

    assert cfg.build_timeout_seconds == 1200
    assert cfg.remote_build_timeout_seconds == 1800


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        (
            "SCREENER_STATIC_PREFLIGHT_V2_MODE",
            "always",
            "off, shadow, or enforce",
        ),
        ("SCREENER_L2_REVIEW_MODE", "always", "off, shadow, or enforce"),
        ("SCREENER_L2_REVIEW_MODEL", "openai/other", "moonshotai/kimi-k3"),
        ("SCREENER_L2_REVIEW_PROVIDER", "azure", "must be openrouter"),
        (
            "SCREENER_L2_FALLBACK_MODELS",
            "openai/gpt-5.6-luna",
            "z-ai/glm-5.2,openai/gpt-5.6-sol",
        ),
        ("SCREENER_L3_REVIEW_MODEL", "openai/gpt-5.6-terra", "gpt-5.6-sol"),
        ("SCREENER_L3_REVIEW_PROVIDER", "azure", "must be openrouter"),
        ("SCREENER_L2_MAX_INPUT_TOKENS", "1000001", "1000000"),
        ("SCREENER_L2_MAX_COST_USD", "20", r"in \(0, 10\]"),
        ("SCREENER_L2_ANALYST_REASONING_EFFORT", "high", "model_default"),
        ("SCREENER_L2_CRITIC_REASONING_EFFORT", "none", "low or medium"),
        (
            "SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS",
            "60",
            "between 300 and 2400",
        ),
        (
            "SCREENER_REMOTE_BUILD_MODE",
            "always",
            "off, prefer, or require",
        ),
    ],
)
def test_l2_safety_configuration_is_bounded(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, match: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ScreenerConfigError, match=match):
        parse_screener_config_from_env()


def test_smoke_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_SMOKE_ENV", "OPENROUTER_API_KEY=k, FOO=bar")
    cfg = parse_screener_config_from_env()
    assert cfg.smoke_env == (("OPENROUTER_API_KEY", "k"), ("FOO", "bar"))


def test_rootless_executor_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_DOCKER_HOST", "unix:///run/user/1001/docker.sock")
    monkeypatch.setenv("SCREENER_REQUIRE_ROOTLESS_DOCKER", "true")
    cfg = parse_screener_config_from_env()
    assert cfg.docker_host == "unix:///run/user/1001/docker.sock"
    assert cfg.require_rootless_docker


def test_smoke_env_bad_pair_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_SMOKE_ENV", "NOEQUALS")
    with pytest.raises(ScreenerConfigError, match="K=V pairs"):
        parse_screener_config_from_env()


def test_missing_signing_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("SCREENER_MNEMONIC", raising=False)
    monkeypatch.delenv("SCREENER_WALLET_NAME", raising=False)
    monkeypatch.delenv("SCREENER_WALLET_HOTKEY", raising=False)
    with pytest.raises(ScreenerConfigError, match="no signing key"):
        parse_screener_config_from_env()


def test_missing_hotkey_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("SCREENER_HOTKEY", raising=False)
    with pytest.raises(ScreenerConfigError, match="SCREENER_HOTKEY"):
        parse_screener_config_from_env()


def test_missing_api_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("SCREENER_API_TOKEN")
    with pytest.raises(ScreenerConfigError, match="SCREENER_API_TOKEN"):
        parse_screener_config_from_env()


def test_missing_source_review_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("SCREENER_SOURCE_REVIEW_API_KEY_FILE")
    with pytest.raises(ScreenerConfigError, match="required by screening policy v8"):
        parse_screener_config_from_env()


def test_short_api_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_API_TOKEN", "too-short")
    with pytest.raises(ScreenerConfigError, match="at least 32 characters"):
        parse_screener_config_from_env()


def test_bad_numeric_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_BUILD_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ScreenerConfigError, match="must be a number"):
        parse_screener_config_from_env()


def test_gh_token_file_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_GH_TOKEN_FILE", "/run/secrets/gh")
    cfg = parse_screener_config_from_env()
    assert cfg.gh_token_file == "/run/secrets/gh"


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_static_preflight_rollout_configuration(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_STATIC_PREFLIGHT_V2_MODE", mode)
    monkeypatch.setenv(
        "SCREENER_STATIC_PREFLIGHT_AUDIT_FILE",
        "/opt/ditto/screener/state/static-preflight.jsonl",
    )

    config = parse_screener_config_from_env()

    assert config.static_preflight_v2_mode == mode
    assert config.static_preflight_audit_file == (
        "/opt/ditto/screener/state/static-preflight.jsonl"
    )


def test_static_preflight_shadow_requires_audit_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_STATIC_PREFLIGHT_V2_MODE", "shadow")

    with pytest.raises(
        ScreenerConfigError,
        match="SCREENER_STATIC_PREFLIGHT_AUDIT_FILE is required in shadow mode",
    ):
        parse_screener_config_from_env()
