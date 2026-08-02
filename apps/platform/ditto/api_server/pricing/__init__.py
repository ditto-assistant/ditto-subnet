"""CoinGecko-backed TAO/USD price oracle for the upload-fee endpoint.

Holds a single in-process cache entry with stale-while-revalidate
semantics. Single-host assumption: multi-host deployments will keep
per-pod caches that converge within the TTL but never share state.
``TAO_PRICE_OVERRIDE_USD`` env var is an operator kill switch that
short-circuits CoinGecko + cache.

Usage:
    from ditto.api_server.pricing import (
        create_price_oracle,
        parse_pricing_config_from_env,
    )

    config = parse_pricing_config_from_env()
    oracle = create_price_oracle(config)
    try:
        tao_usd = await oracle.get_tao_usd()
    finally:
        await oracle.aclose()
"""

from __future__ import annotations

from ditto.api_server.pricing.config import (
    PricingConfig,
    parse_pricing_config_from_env,
)
from ditto.api_server.pricing.errors import (
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
    PricingError,
)
from ditto.api_server.pricing.factory import create_price_oracle
from ditto.api_server.pricing.oracle import CoinGeckoOracle, PriceOracle

__all__ = [
    "CoinGeckoOracle",
    "MalformedPriceError",
    "OracleUnreachableError",
    "PriceOracle",
    "PriceTooStaleError",
    "PricingConfig",
    "PricingError",
    "create_price_oracle",
    "parse_pricing_config_from_env",
]
