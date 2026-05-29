"""Factory for the pricing oracle."""

from __future__ import annotations

import httpx

from ditto.api_server.pricing.config import PricingConfig
from ditto.api_server.pricing.oracle import CoinGeckoOracle


def create_price_oracle(config: PricingConfig) -> CoinGeckoOracle:
    """Open an :class:`httpx.AsyncClient` and return a configured oracle.

    Caller owns lifecycle: ``await oracle.aclose()`` once finished. The
    api_server lifespan registers it on the ``AsyncExitStack`` so a
    failed startup still releases the underlying connection pool.
    """
    client = httpx.AsyncClient(
        timeout=config.coingecko_timeout_seconds,
        headers={"User-Agent": "ditto-api-server/0.0.1"},
    )
    return CoinGeckoOracle(config, client)
