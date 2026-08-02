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


def _require_positive_finite_decimal(name: str, value: Decimal) -> Decimal:
    if value.is_nan() or value.is_infinite() or value <= 0:
        raise PricingError(f"{name} must be a positive finite decimal, got {value}")
    return value


def _require_positive_int(name: str, value: int) -> int:
    if value <= 0:
        raise PricingError(f"{name} must be a positive integer, got {value}")
    return value


def _require_positive_finite_float(name: str, value: float) -> float:
    # NaN != NaN is the canonical NaN check; isinf catches +/- inf.
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        raise PricingError(f"{name} must be a positive finite float, got {value}")
    return value


def _parse_override(name: str, raw: str | None) -> Decimal | None:
    """Parse the operator override; rejects non-positive or non-finite values."""
    value = _parse_optional_decimal(name, raw)
    if value is None:
        return None
    return _require_positive_finite_decimal(name, value)


def parse_pricing_config_from_env() -> PricingConfig:
    """Build :class:`PricingConfig` from ``DITTO_UPLOAD_*`` + ``PRICING_*`` env vars.

    Every numeric field is validated positive + finite at boot so a typo
    (``PRICING_CACHE_TTL_SECONDS=-100``, ``PRICING_COINGECKO_TIMEOUT_SECONDS=NaN``)
    refuses to start instead of degrading the endpoint at request time.

    Raises:
        PricingError: When a numeric env var cannot be parsed or is not
            a positive finite number.
    """
    try:
        return PricingConfig(
            fee_usd=_require_positive_finite_decimal(
                "DITTO_UPLOAD_FEE_USD",
                _parse_decimal(
                    "DITTO_UPLOAD_FEE_USD",
                    os.environ.get("DITTO_UPLOAD_FEE_USD", "5"),
                ),
            ),
            fee_buffer=_require_positive_finite_decimal(
                "DITTO_UPLOAD_FEE_BUFFER",
                _parse_decimal(
                    "DITTO_UPLOAD_FEE_BUFFER",
                    os.environ.get("DITTO_UPLOAD_FEE_BUFFER", "1.4"),
                ),
            ),
            cache_ttl_seconds=_require_positive_int(
                "PRICING_CACHE_TTL_SECONDS",
                int(os.environ.get("PRICING_CACHE_TTL_SECONDS", "3600")),
            ),
            max_stale_seconds=_require_positive_int(
                "PRICING_MAX_STALE_SECONDS",
                int(os.environ.get("PRICING_MAX_STALE_SECONDS", "86400")),
            ),
            coingecko_timeout_seconds=_require_positive_finite_float(
                "PRICING_COINGECKO_TIMEOUT_SECONDS",
                float(os.environ.get("PRICING_COINGECKO_TIMEOUT_SECONDS", "5.0")),
            ),
            override_tao_usd=_parse_override(
                "TAO_PRICE_OVERRIDE_USD",
                os.environ.get("TAO_PRICE_OVERRIDE_USD"),
            ),
        )
    except ValueError as e:
        raise PricingError(f"invalid numeric pricing env var: {e}") from e
