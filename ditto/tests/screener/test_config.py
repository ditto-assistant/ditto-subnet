"""Tests for the screener env-driven config."""

from __future__ import annotations

import pytest

from ditto.screener.config import parse_screener_config_from_env
from ditto.screener.errors import ScreenerConfigError

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_MNEMONIC = "bottom drive obey lake curtain smoke basket hold race lonely fit walk"


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENER_HOTKEY", _HOTKEY)
    monkeypatch.setenv("SCREENER_MNEMONIC", _MNEMONIC)
    for k in (
        "SCREENER_WALLET_NAME",
        "SCREENER_WALLET_HOTKEY",
        "SCREENER_GH_TOKEN_FILE",
        "SCREENER_BUILD_TIMEOUT_SECONDS",
        "NETUID",
    ):
        monkeypatch.delenv(k, raising=False)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    cfg = parse_screener_config_from_env()
    assert cfg.screener_hotkey == _HOTKEY
    assert cfg.netuid == 118
    assert cfg.docker_bin == "docker"
    assert cfg.container_port == 8080
    assert cfg.gh_token_file is None
    # Must default to (at least) the platform's 20 MiB upload cap, else the gate
    # false-fails legitimately-uploaded tarballs.
    assert cfg.max_tarball_bytes == 20 * 1024 * 1024
    # A dummy LLM key is injected by default so the reference harness (which
    # builds its OpenRouter Baseline before binding /health) boots during the
    # serve smoke.
    assert cfg.smoke_env == (("OPENROUTER_API_KEY", "sk-screener-smoke"),)
    assert cfg.signing_source_present()


def test_smoke_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_SMOKE_ENV", "OPENROUTER_API_KEY=k, FOO=bar")
    cfg = parse_screener_config_from_env()
    assert cfg.smoke_env == (("OPENROUTER_API_KEY", "k"), ("FOO", "bar"))


def test_smoke_env_bad_pair_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("SCREENER_SMOKE_ENV", "NOEQUALS")
    with pytest.raises(ScreenerConfigError, match="K=V pairs"):
        parse_screener_config_from_env()


def test_missing_signing_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCREENER_HOTKEY", _HOTKEY)
    monkeypatch.delenv("SCREENER_MNEMONIC", raising=False)
    monkeypatch.delenv("SCREENER_WALLET_NAME", raising=False)
    monkeypatch.delenv("SCREENER_WALLET_HOTKEY", raising=False)
    with pytest.raises(ScreenerConfigError, match="no signing key"):
        parse_screener_config_from_env()


def test_missing_hotkey_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCREENER_HOTKEY", raising=False)
    monkeypatch.setenv("SCREENER_MNEMONIC", _MNEMONIC)
    with pytest.raises(ScreenerConfigError, match="SCREENER_HOTKEY"):
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
