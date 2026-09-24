"""Unit tests for :mod:`ditto.api_server.pricing.factory`."""

from __future__ import annotations

from ditto.api_server.pricing import (
    CoinGeckoOracle,
    PricingConfig,
    create_price_oracle,
)


def _config() -> PricingConfig:
    return PricingConfig(
        cache_ttl_seconds=60,
        max_stale_seconds=300,
        coingecko_timeout_seconds=1.0,
        override_tao_usd=None,
    )


class TestCreatePriceOracle:
    async def test_returns_coingecko_oracle(self):
        oracle = create_price_oracle(_config())
        try:
            assert isinstance(oracle, CoinGeckoOracle)
        finally:
            await oracle.aclose()
