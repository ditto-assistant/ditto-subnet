"""Unit tests for :mod:`ditto.api_server.pricing.config`."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ditto.api_server.pricing import (
    PricingError,
    parse_pricing_config_from_env,
)


def _clear_pricing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DITTO_UPLOAD_FEE_USD",
        "DITTO_UPLOAD_FEE_BUFFER",
        "PRICING_CACHE_TTL_SECONDS",
        "PRICING_MAX_STALE_SECONDS",
        "PRICING_COINGECKO_TIMEOUT_SECONDS",
        "TAO_PRICE_OVERRIDE_USD",
    ):
        monkeypatch.delenv(key, raising=False)


class TestParsePricingConfigFromEnv:
    def test_defaults_apply_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)

        config = parse_pricing_config_from_env()

        assert config.fee_usd == Decimal("5")
        assert config.fee_buffer == Decimal("1.4")
        assert config.cache_ttl_seconds == 3600
        assert config.max_stale_seconds == 86400
        assert config.coingecko_timeout_seconds == 5.0
        assert config.override_tao_usd is None

    def test_all_options_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_FEE_USD", "7.50")
        monkeypatch.setenv("DITTO_UPLOAD_FEE_BUFFER", "1.2")
        monkeypatch.setenv("PRICING_CACHE_TTL_SECONDS", "60")
        monkeypatch.setenv("PRICING_MAX_STALE_SECONDS", "120")
        monkeypatch.setenv("PRICING_COINGECKO_TIMEOUT_SECONDS", "2.5")
        monkeypatch.setenv("TAO_PRICE_OVERRIDE_USD", "999.99")

        config = parse_pricing_config_from_env()

        assert config.fee_usd == Decimal("7.50")
        assert config.fee_buffer == Decimal("1.2")
        assert config.cache_ttl_seconds == 60
        assert config.max_stale_seconds == 120
        assert config.coingecko_timeout_seconds == 2.5
        assert config.override_tao_usd == Decimal("999.99")

    def test_empty_override_treated_as_none(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("TAO_PRICE_OVERRIDE_USD", "")

        config = parse_pricing_config_from_env()

        assert config.override_tao_usd is None

    def test_invalid_decimal_raises(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_FEE_USD", "not-a-number")

        with pytest.raises(PricingError, match="DITTO_UPLOAD_FEE_USD"):
            parse_pricing_config_from_env()

    def test_invalid_int_raises(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("PRICING_CACHE_TTL_SECONDS", "abc")

        with pytest.raises(PricingError, match="invalid numeric"):
            parse_pricing_config_from_env()

    @pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "0", "-1"])
    def test_invalid_override_raises(self, monkeypatch: pytest.MonkeyPatch, bad: str):
        """The kill switch bypasses cache + validation, so the gate must
        live at parse time. Without this, ``TAO_PRICE_OVERRIDE_USD=0``
        crashes the endpoint with a DivisionByZero in handler scope."""
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("TAO_PRICE_OVERRIDE_USD", bad)

        with pytest.raises(PricingError, match="positive finite"):
            parse_pricing_config_from_env()
