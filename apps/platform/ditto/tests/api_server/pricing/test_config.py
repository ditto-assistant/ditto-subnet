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

        assert config.cache_ttl_seconds == 3600
        assert config.max_stale_seconds == 86400
        assert config.coingecko_timeout_seconds == 5.0
        assert config.override_tao_usd is None

    def test_all_options_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("PRICING_CACHE_TTL_SECONDS", "60")
        monkeypatch.setenv("PRICING_MAX_STALE_SECONDS", "120")
        monkeypatch.setenv("PRICING_COINGECKO_TIMEOUT_SECONDS", "2.5")
        monkeypatch.setenv("TAO_PRICE_OVERRIDE_USD", "999.99")

        config = parse_pricing_config_from_env()

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
        monkeypatch.setenv("TAO_PRICE_OVERRIDE_USD", "not-a-number")

        with pytest.raises(PricingError, match="TAO_PRICE_OVERRIDE_USD"):
            parse_pricing_config_from_env()

    def test_retired_usd_fee_variables_have_no_pricing_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The fee is the revisioned fixed-TAO DB policy, never a deploy knob.

        A leftover deployment value (even a malformed one) must neither be
        parsed nor surface on the config, so it cannot silently re-become a
        USD-denominated fee.
        """
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("DITTO_UPLOAD_FEE_USD", "not-a-number")
        monkeypatch.setenv("DITTO_UPLOAD_FEE_BUFFER", "-1")

        config = parse_pricing_config_from_env()

        assert not hasattr(config, "fee_usd")
        assert not hasattr(config, "fee_buffer")

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

    @pytest.mark.parametrize(
        ("env_var", "bad"),
        [
            ("PRICING_CACHE_TTL_SECONDS", "0"),
            ("PRICING_CACHE_TTL_SECONDS", "-100"),
            ("PRICING_MAX_STALE_SECONDS", "0"),
            ("PRICING_MAX_STALE_SECONDS", "-1"),
        ],
    )
    def test_invalid_int_env_var_raises(
        self, monkeypatch: pytest.MonkeyPatch, env_var: str, bad: str
    ):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv(env_var, bad)

        with pytest.raises(PricingError, match=f"{env_var}.*positive integer"):
            parse_pricing_config_from_env()

    @pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "-inf"])
    def test_invalid_timeout_raises(self, monkeypatch: pytest.MonkeyPatch, bad: str):
        _clear_pricing_env(monkeypatch)
        monkeypatch.setenv("PRICING_COINGECKO_TIMEOUT_SECONDS", bad)

        with pytest.raises(
            PricingError,
            match="PRICING_COINGECKO_TIMEOUT_SECONDS.*positive finite",
        ):
            parse_pricing_config_from_env()
