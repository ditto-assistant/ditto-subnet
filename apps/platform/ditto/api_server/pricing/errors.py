"""Exception hierarchy for the pricing oracle."""

from __future__ import annotations


class PricingError(Exception):
    """Base exception for :mod:`ditto.api_server.pricing`."""


# --- Oracle availability ---


class OracleUnreachableError(PricingError):
    """Raised when the upstream price oracle cannot be reached.

    This can happen when:
    - CoinGecko returns a 5xx response or rate-limits the request.
    - DNS or network failure blocks the outbound call.
    - The configured timeout fires before a response arrives.
    - All tenacity retry attempts fail and no cached value exists.
    """


class PriceTooStaleError(PricingError):
    """Raised when the cached price exceeds ``PRICING_MAX_STALE_SECONDS``.

    This can happen when:
    - CoinGecko has been unreachable for longer than the configured
      max-staleness window (default 24 h).
    - The process has been running through a multi-day upstream
      outage without operator intervention via
      ``TAO_PRICE_OVERRIDE_USD``.
    """


# --- Oracle correctness ---


class MalformedPriceError(PricingError):
    """Raised when the oracle returns a price that is not a positive finite number.

    This can happen when:
    - CoinGecko returns ``null``, ``0``, or a negative value.
    - The response JSON does not match the expected shape.
    - A floating-point value comes back as ``NaN`` or ``Infinity``.
    """
