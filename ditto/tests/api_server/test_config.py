"""Unit tests for :mod:`ditto.api_server.config`."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ditto.api_server.config import check_config, parse_api_server_config_from_env
from ditto.api_server.errors import ApiServerConfigError
from ditto.tests.api_server.conftest import make_api_server_config


def _set_minimum_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars to make both sub-config parsers succeed."""
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok")


class TestParseApiServerConfigFromEnv:
    """Tests for the env-var builder."""

    def test_defaults_apply_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.delenv("API_HOST", raising=False)
        monkeypatch.delenv("API_PORT", raising=False)
        monkeypatch.delenv("API_LOG_LEVEL", raising=False)

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.host == "0.0.0.0"
        assert config.port == 8000
        assert config.log_level == "INFO"
        assert config.commit_hash == "abc"

    def test_overrides_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("API_HOST", "127.0.0.1")
        monkeypatch.setenv("API_PORT", "9000")
        monkeypatch.setenv("API_LOG_LEVEL", "debug")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.host == "127.0.0.1"
        assert config.port == 9000
        assert config.log_level == "DEBUG"

    def test_composition_with_sub_configs(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("POSTGRES_USER", "alice")
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok-xyz")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.postgres.user == "alice"
        assert config.chain.open_access_token == "tok-xyz"

    def test_non_integer_port_raises(self, monkeypatch: pytest.MonkeyPatch):
        """Parse-time failure: the value is not coercible to int."""
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("API_PORT", "not-a-port")

        with pytest.raises(ApiServerConfigError, match="API_PORT"):
            parse_api_server_config_from_env(commit_hash="abc")


class TestCheckConfig:
    """Validation gates that the dataclass type system cannot enforce."""

    def test_valid_config_passes(self):
        check_config(make_api_server_config())

    def test_port_out_of_range_raises(self):
        config = replace(make_api_server_config(), port=0)
        with pytest.raises(ApiServerConfigError, match="port out of range"):
            check_config(config)

    def test_port_above_max_raises(self):
        config = replace(make_api_server_config(), port=70000)
        with pytest.raises(ApiServerConfigError, match="port out of range"):
            check_config(config)

    def test_unknown_log_level_raises(self):
        config = replace(make_api_server_config(), log_level="loud")
        with pytest.raises(ApiServerConfigError, match="log_level"):
            check_config(config)
