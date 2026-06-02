"""Unit tests for :mod:`ditto.api_server.__main__`."""

from __future__ import annotations

import argparse
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ditto.api_server.__main__ import (
    _config_from_args,
    _config_to_log_dict,
    _redact,
    _resolve_commit_hash,
)


def _make_args(**overrides: object) -> argparse.Namespace:
    """Build an argparse-like namespace for ``_config_from_args``."""
    base = {"host": "127.0.0.1", "port": 9000, "log_level": "debug"}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestResolveCommitHash:
    def test_returns_hex_on_success(self):
        result = MagicMock(returncode=0, stdout="abcdef1234567890\n")
        with patch("ditto.api_server.__main__.subprocess.run", return_value=result):
            assert _resolve_commit_hash() == "abcdef1234567890"

    def test_non_zero_exit_falls_back_to_unknown(self):
        result = MagicMock(returncode=128, stdout="")
        with patch("ditto.api_server.__main__.subprocess.run", return_value=result):
            assert _resolve_commit_hash() == "unknown"

    def test_empty_stdout_falls_back_to_unknown(self):
        result = MagicMock(returncode=0, stdout="\n")
        with patch("ditto.api_server.__main__.subprocess.run", return_value=result):
            assert _resolve_commit_hash() == "unknown"

    def test_file_not_found_falls_back_to_unknown(self):
        with patch(
            "ditto.api_server.__main__.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            assert _resolve_commit_hash() == "unknown"

    def test_timeout_falls_back_to_unknown(self):
        with patch(
            "ditto.api_server.__main__.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
        ):
            assert _resolve_commit_hash() == "unknown"


class TestConfigFromArgs:
    """argparse Namespace overlay on env-resolved config."""

    def test_overlays_host_port_log_level(self, monkeypatch: pytest.MonkeyPatch):
        # Minimum env so sub-config parsers succeed.
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DB", "d")
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok")
        monkeypatch.setenv(
            "DITTO_UPLOAD_PAYMENT_ADDRESS",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("STORAGE_BUCKET", "ditto-agents")
        monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio")
        monkeypatch.setenv("STORAGE_SECRET_KEY", "miniominio")
        monkeypatch.delenv("API_HOST", raising=False)
        monkeypatch.delenv("API_PORT", raising=False)
        monkeypatch.delenv("API_LOG_LEVEL", raising=False)

        with patch(
            "ditto.api_server.__main__._resolve_commit_hash",
            return_value="abc1234",
        ):
            config = _config_from_args(
                _make_args(host="0.0.0.0", port=8080, log_level="warning")
            )

        assert config.host == "0.0.0.0"
        assert config.port == 8080
        assert config.log_level == "WARNING"
        assert config.commit_hash == "abc1234"

    def test_preserves_env_resolved_sub_configs(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("POSTGRES_USER", "alice")
        monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
        monkeypatch.setenv("POSTGRES_DB", "metrics")
        monkeypatch.setenv("PYLON_OPEN_ACCESS_TOKEN", "tok-x")
        monkeypatch.setenv(
            "DITTO_UPLOAD_PAYMENT_ADDRESS",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        )
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
        monkeypatch.setenv("STORAGE_BUCKET", "ditto-agents")
        monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio")
        monkeypatch.setenv("STORAGE_SECRET_KEY", "miniominio")

        with patch(
            "ditto.api_server.__main__._resolve_commit_hash",
            return_value="x",
        ):
            config = _config_from_args(_make_args())

        assert config.postgres.user == "alice"
        assert config.postgres.database == "metrics"
        assert config.chain.open_access_token == "tok-x"
        assert config.storage.bucket == "ditto-agents"


class TestRedact:
    """Redaction helper for boot-time logging."""

    def test_short_strings_fully_masked(self):
        assert _redact("abc") == "***"
        assert _redact("abcd") == "***"

    def test_long_string_keeps_tail(self):
        assert _redact("supersecrettoken") == "***oken"

    def test_none_renders_unset(self):
        assert _redact(None) == "<unset>"

    def test_empty_string_renders_unset(self):
        assert _redact("") == "<unset>"

    def test_custom_keep_length(self):
        assert _redact("abcdefgh", keep=2) == "***gh"


class TestConfigToLogDict:
    """Boot-time config echo redacts secrets but exposes wiring."""

    def test_includes_api_postgres_and_chain_sections(self):
        from ditto.tests.api_server.conftest import make_api_server_config

        config = make_api_server_config()
        echo = _config_to_log_dict(config)

        assert "api" in echo
        assert "postgres" in echo
        assert "chain" in echo

    def test_postgres_password_redacted(self):
        from ditto.tests.api_server.conftest import make_api_server_config

        config = make_api_server_config()
        echo = _config_to_log_dict(config)
        # The fixture sets password="ditto" - must not appear raw.
        assert "ditto" not in echo["postgres"]["password"]
        assert echo["postgres"]["password"].startswith("***")

    def test_open_access_token_redacted(self):
        from ditto.tests.api_server.conftest import make_api_server_config

        config = make_api_server_config()
        echo = _config_to_log_dict(config)
        assert echo["chain"]["open_access_token"].startswith("***")

    def test_host_port_db_exposed(self):
        from ditto.tests.api_server.conftest import make_api_server_config

        config = make_api_server_config()
        echo = _config_to_log_dict(config)
        assert echo["postgres"]["host"] == "localhost"
        assert echo["postgres"]["database"] == "ditto"
        assert echo["chain"]["pylon_url"] == "http://pylon:8001"
        assert echo["chain"]["netuid"] == 118
