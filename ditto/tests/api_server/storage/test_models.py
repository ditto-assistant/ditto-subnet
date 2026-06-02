"""Unit tests for :mod:`ditto.api_server.storage.models`."""

from __future__ import annotations

import pytest

from ditto.api_server.storage import (
    StorageConfigurationError,
    parse_storage_config_from_env,
)

_STORAGE_ENV_VARS = (
    "STORAGE_ENDPOINT_URL",
    "STORAGE_BUCKET",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "STORAGE_REGION",
    "STORAGE_USE_TLS",
)


def _clear_storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _STORAGE_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("STORAGE_BUCKET", "ditto-agents")
    monkeypatch.setenv("STORAGE_ACCESS_KEY", "minio")
    monkeypatch.setenv("STORAGE_SECRET_KEY", "miniominio")


class TestParseStorageConfigFromEnv:
    def test_required_fields_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _clear_storage_env(monkeypatch)
        _set_required(monkeypatch)

        config = parse_storage_config_from_env()

        assert config.endpoint_url == "http://minio:9000"
        assert config.bucket == "ditto-agents"
        assert config.access_key == "minio"
        assert config.secret_key == "miniominio"
        assert config.region == "us-east-1"
        assert config.use_tls is False

    def test_optional_overrides(self, monkeypatch: pytest.MonkeyPatch):
        _clear_storage_env(monkeypatch)
        _set_required(monkeypatch)
        monkeypatch.setenv("STORAGE_REGION", "eu-west-1")
        monkeypatch.setenv("STORAGE_USE_TLS", "true")

        config = parse_storage_config_from_env()

        assert config.region == "eu-west-1"
        assert config.use_tls is True

    @pytest.mark.parametrize(
        "missing",
        [
            "STORAGE_ENDPOINT_URL",
            "STORAGE_BUCKET",
            "STORAGE_ACCESS_KEY",
            "STORAGE_SECRET_KEY",
        ],
    )
    def test_missing_required_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ):
        _clear_storage_env(monkeypatch)
        _set_required(monkeypatch)
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(StorageConfigurationError, match=missing):
            parse_storage_config_from_env()

    def test_invalid_use_tls_raises(self, monkeypatch: pytest.MonkeyPatch):
        _clear_storage_env(monkeypatch)
        _set_required(monkeypatch)
        monkeypatch.setenv("STORAGE_USE_TLS", "maybe")

        with pytest.raises(StorageConfigurationError, match="STORAGE_USE_TLS"):
            parse_storage_config_from_env()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("FALSE", False),
            ("0", False),
            ("no", False),
            ("off", False),
        ],
    )
    def test_use_tls_bool_parsing(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ):
        _clear_storage_env(monkeypatch)
        _set_required(monkeypatch)
        monkeypatch.setenv("STORAGE_USE_TLS", raw)

        config = parse_storage_config_from_env()

        assert config.use_tls is expected
