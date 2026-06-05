"""Unit tests for :mod:`ditto.api_server.config`."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ditto.api_server.config import check_config, parse_api_server_config_from_env
from ditto.api_server.errors import ApiServerConfigError
from ditto.tests.api_server.conftest import make_api_server_config


def _set_minimum_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars to make every sub-config parser succeed."""
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
    # Override unset by default; tested explicitly elsewhere.
    monkeypatch.delenv("TAO_PRICE_OVERRIDE_USD", raising=False)


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

    def test_missing_payment_address_raises(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.delenv("DITTO_UPLOAD_PAYMENT_ADDRESS", raising=False)

        with pytest.raises(ApiServerConfigError, match="DITTO_UPLOAD_PAYMENT_ADDRESS"):
            parse_api_server_config_from_env(commit_hash="abc")

    @pytest.mark.parametrize(
        "bad",
        [
            "REPLACE_WITH_DITTO_SS58_ADDRESS",  # the .env.example placeholder
            "not-an-ss58",
            "5short",
            "0OIl" * 12 + "1234567890ab",  # SS58 forbidden chars 0/O/I/l
        ],
    )
    def test_malformed_payment_address_raises(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_PAYMENT_ADDRESS", bad)

        with pytest.raises(ApiServerConfigError, match="SS58"):
            parse_api_server_config_from_env(commit_hash="abc")

    def test_pricing_sub_config_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_FEE_USD", "7.50")
        monkeypatch.setenv("DITTO_UPLOAD_FEE_BUFFER", "1.2")
        monkeypatch.setenv("PRICING_CACHE_TTL_SECONDS", "60")

        config = parse_api_server_config_from_env(commit_hash="abc")

        from decimal import Decimal

        assert config.pricing.fee_usd == Decimal("7.50")
        assert config.pricing.fee_buffer == Decimal("1.2")
        assert config.pricing.cache_ttl_seconds == 60

    def test_storage_sub_config_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _set_minimum_env(monkeypatch)
        monkeypatch.setenv("STORAGE_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.setenv("STORAGE_BUCKET", "custom-bucket")
        monkeypatch.setenv("STORAGE_REGION", "eu-west-1")
        monkeypatch.setenv("STORAGE_USE_TLS", "true")

        config = parse_api_server_config_from_env(commit_hash="abc")

        assert config.storage.endpoint_url == "https://s3.example.com"
        assert config.storage.bucket == "custom-bucket"
        assert config.storage.region == "eu-west-1"
        assert config.storage.use_tls is True


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
