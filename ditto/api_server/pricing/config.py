"""Configuration for the pricing oracle."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ditto.api_server.pricing.errors import PricingError


@dataclass(frozen=True)
class PricingConfig:
    """Resolved configuration for :class:`CoinGeckoOracle`.

    Decimal fields use :class:`decimal.Decimal` end-to-end so a
    ``× buffer × 1e9`` arithmetic chain never picks up float drift.
    """

    fee_usd: Decimal
    """Upload fee denominated in USD (``DITTO_UPLOAD_FEE_USD``)."""

    fee_buffer: Decimal
    """Multiplier covering TAO/USD drift between fee quote and on-chain
    payment (``DITTO_UPLOAD_FEE_BUFFER``)."""

    cache_ttl_seconds: int
    """Fresh-cache TTL (``PRICING_CACHE_TTL_SECONDS``)."""

    max_stale_seconds: int
    """Hard cap on stale-while-revalidate before refusing to serve
    (``PRICING_MAX_STALE_SECONDS``)."""

    coingecko_timeout_seconds: float
    """Per-attempt CoinGecko HTTP timeout
    (``PRICING_COINGECKO_TIMEOUT_SECONDS``)."""

    override_tao_usd: Decimal | None
    """Operator kill switch (``TAO_PRICE_OVERRIDE_USD``). When set, the
    oracle skips CoinGecko + cache and returns this value verbatim.
    Migrates to the ``internal_flags`` table when that lands."""


def _parse_decimal(name: str, raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation as e:
        raise PricingError(f"{name} must be a decimal number, got {raw!r}") from e


def _parse_optional_decimal(name: str, raw: str | None) -> Decimal | None:
    if raw is None or raw == "":
        return None
    return _parse_decimal(name, raw)


def _parse_override(name: str, raw: str | None) -> Decimal | None:
    """Parse the operator override and reject non-finite or non-positive values.

    The kill switch bypasses CoinGecko + cache + the price-validation
    code that would otherwise reject these, so the gate has to live
    here. Fails closed so a typo (``TAO_PRICE_OVERRIDE_USD=Infinity``)
    refuses boot instead of silently breaking the endpoint.
    """
    value = _parse_optional_decimal(name, raw)
    if value is None:
        return None
    if value.is_nan() or value.is_infinite() or value <= 0:
        raise PricingError(f"{name} must be a positive finite decimal, got {value}")
    return value


def parse_pricing_config_from_env() -> PricingConfig:
    """Build :class:`PricingConfig` from ``DITTO_UPLOAD_*`` + ``PRICING_*`` env vars.

    Raises:
        PricingError: When a numeric env var cannot be parsed.
    """
    try:
        return PricingConfig(
            fee_usd=_parse_decimal(
                "DITTO_UPLOAD_FEE_USD",
                os.environ.get("DITTO_UPLOAD_FEE_USD", "5"),
            ),
            fee_buffer=_parse_decimal(
                "DITTO_UPLOAD_FEE_BUFFER",
                os.environ.get("DITTO_UPLOAD_FEE_BUFFER", "1.4"),
            ),
            cache_ttl_seconds=int(os.environ.get("PRICING_CACHE_TTL_SECONDS", "3600")),
            max_stale_seconds=int(os.environ.get("PRICING_MAX_STALE_SECONDS", "86400")),
            coingecko_timeout_seconds=float(
                os.environ.get("PRICING_COINGECKO_TIMEOUT_SECONDS", "5.0")
            ),
            override_tao_usd=_parse_override(
                "TAO_PRICE_OVERRIDE_USD",
                os.environ.get("TAO_PRICE_OVERRIDE_USD"),
            ),
        )
    except ValueError as e:
        raise PricingError(f"invalid numeric pricing env var: {e}") from e
