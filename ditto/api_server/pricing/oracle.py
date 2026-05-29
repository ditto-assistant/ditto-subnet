"""CoinGecko-backed TAO/USD price oracle with stale-while-revalidate.

The oracle holds a single in-process cache entry. Single-host
deployments are the assumption; multi-host introduces per-pod caches
that converge within a TTL but never share state.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from ditto.api_server.pricing.errors import (
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
)

if TYPE_CHECKING:
    from ditto.api_server.pricing.config import PricingConfig

logger = logging.getLogger(__name__)

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
)


class PriceOracle(Protocol):
    """Surface every endpoint relies on."""

    async def get_tao_usd(self) -> Decimal: ...

    async def aclose(self) -> None: ...


class CoinGeckoOracle:
    """In-process cached TAO/USD price with stale-while-revalidate.

    Cache: single ``(price, fetched_at_unix)`` tuple. A fresh fetch is
    attempted when the cache is older than ``cache_ttl_seconds``; on
    failure the stale value is served until ``max_stale_seconds`` is
    exceeded.

    ``asyncio.Lock`` serialises concurrent refresh attempts so a
    thundering herd on a cold cache hits CoinGecko exactly once.
    """

    def __init__(self, config: PricingConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._cache: tuple[Decimal, float] | None = None
        self._fetch_lock = asyncio.Lock()

    async def get_tao_usd(self) -> Decimal:
        """Return the current TAO/USD price as :class:`Decimal`.

        Order of operations:
        1. If the operator override is set, return it verbatim.
        2. If the cache is fresh, return it.
        3. Otherwise acquire the lock and attempt a fresh fetch.
        4. On fetch failure, serve stale if still within max-stale.
        5. Otherwise raise.

        Raises:
            OracleUnreachableError: When fetch fails and no cache exists.
            PriceTooStaleError: When cache exceeds ``max_stale_seconds``.
            MalformedPriceError: When CoinGecko returns a non-positive
                or non-finite value.
        """
        if self._config.override_tao_usd is not None:
            return self._config.override_tao_usd

        now = time.time()
        cached = self._cache
        if cached is not None:
            price, fetched_at = cached
            if now - fetched_at < self._config.cache_ttl_seconds:
                return price

        async with self._fetch_lock:
            # Double-checked: another task may have refreshed while we waited.
            cached = self._cache
            if cached is not None:
                price, fetched_at = cached
                if time.time() - fetched_at < self._config.cache_ttl_seconds:
                    return price

            try:
                price = await self._fetch_with_retry()
            except (httpx.HTTPError, RetryError) as e:
                return self._fall_back_or_raise(e)

            self._cache = (price, time.time())
            return price

    async def _fetch_with_retry(self) -> Decimal:
        """Hit CoinGecko with bounded retry. Validates the returned price."""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.TimeoutException)
            ),
            reraise=True,
        ):
            with attempt:
                response = await self._client.get(
                    COINGECKO_URL,
                    timeout=self._config.coingecko_timeout_seconds,
                )
                response.raise_for_status()
                # CoinGecko has been known to return 200 with an HTML
                # error page during CDN incidents; wrap the decode so
                # the failure surfaces as MalformedPriceError instead
                # of an unhandled ValueError.
                try:
                    payload = response.json()
                except ValueError as e:
                    raise MalformedPriceError(
                        "CoinGecko response body is not valid JSON"
                    ) from e
        return self._extract_price(payload)

    def _fall_back_or_raise(self, cause: Exception) -> Decimal:
        """Return stale cache if within window, else raise the right typed error."""
        cached = self._cache
        if cached is None:
            raise OracleUnreachableError(
                "CoinGecko unreachable, no cached price"
            ) from cause
        price, fetched_at = cached
        age = time.time() - fetched_at
        if age >= self._config.max_stale_seconds:
            raise PriceTooStaleError(
                f"cache age {age:.0f}s exceeds max stale "
                f"{self._config.max_stale_seconds}s"
            ) from cause
        logger.warning(
            f"serving stale TAO/USD price (age {age:.0f}s); "
            f"CoinGecko unreachable: {cause}"
        )
        return price

    @staticmethod
    def _extract_price(payload: object) -> Decimal:
        """Walk the CoinGecko JSON, return the price as Decimal, validate."""
        if not isinstance(payload, dict):
            raise MalformedPriceError(
                f"unexpected payload shape: {type(payload).__name__}"
            )
        bittensor = payload.get("bittensor")
        if not isinstance(bittensor, dict):
            raise MalformedPriceError("payload missing 'bittensor' object")
        raw = bittensor.get("usd")
        if raw is None:
            raise MalformedPriceError("payload missing 'bittensor.usd'")
        try:
            # str() first to avoid float-binary surprises in Decimal.
            price = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as e:
            raise MalformedPriceError(f"price {raw!r} not parseable as Decimal") from e
        # Decimal supports is_nan + is_infinite; also reject non-positive.
        if price.is_nan() or price.is_infinite() or price <= 0:
            raise MalformedPriceError(f"price {price} is not a positive finite number")
        # Guard against finite-but-absurd floats that slipped through (rare).
        try:
            if math.isinf(float(price)) or math.isnan(float(price)):
                raise MalformedPriceError(
                    f"price {price} is not finite under float cast"
                )
        except OverflowError as e:
            raise MalformedPriceError(f"price {price} overflows float") from e
        return price

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
