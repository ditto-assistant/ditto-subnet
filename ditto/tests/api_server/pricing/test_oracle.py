"""Unit tests for :mod:`ditto.api_server.pricing.oracle`.

CoinGecko is mocked at the transport layer via :class:`httpx.MockTransport`
so no real HTTP traffic leaves the process during the test run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from ditto.api_server.pricing import (
    CoinGeckoOracle,
    MalformedPriceError,
    OracleUnreachableError,
    PriceTooStaleError,
    PricingConfig,
)


def make_pricing_config(**overrides: Any) -> PricingConfig:
    base = PricingConfig(
        fee_usd=Decimal("5"),
        fee_buffer=Decimal("1.4"),
        cache_ttl_seconds=60,
        max_stale_seconds=300,
        coingecko_timeout_seconds=1.0,
        override_tao_usd=None,
    )
    if overrides:
        from dataclasses import replace

        return replace(base, **overrides)
    return base


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_response(price_usd: float | str) -> httpx.Response:
    return httpx.Response(200, json={"bittensor": {"usd": price_usd}})


class TestOverride:
    async def test_override_short_circuits_everything(self):
        """When the kill switch is set, the oracle skips cache + CoinGecko."""

        # Set a handler that would fail if called, to prove it isn't.
        def boom(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("CoinGecko should NOT have been called")

        config = make_pricing_config(override_tao_usd=Decimal("123.45"))
        async with make_client(boom) as client:
            oracle = CoinGeckoOracle(config, client)
            assert await oracle.get_tao_usd() == Decimal("123.45")
            # Second call still works without hitting the network.
            assert await oracle.get_tao_usd() == Decimal("123.45")


class TestFreshFetchAndCache:
    async def test_first_call_fetches_second_uses_cache(self):
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return ok_response("400.00")

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            assert await oracle.get_tao_usd() == Decimal("400.00")
            assert await oracle.get_tao_usd() == Decimal("400.00")
        assert call_count == 1

    async def test_cache_refresh_after_ttl(self):
        call_count = 0
        prices = ["400.00", "401.00"]

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return ok_response(prices[call_count - 1])

        # Sub-second TTL so we don't have to sleep long.
        config = make_pricing_config(cache_ttl_seconds=0)
        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(config, client)
            assert await oracle.get_tao_usd() == Decimal("400.00")
            # ttl=0 means every call is past TTL and triggers a refetch.
            assert await oracle.get_tao_usd() == Decimal("401.00")
        assert call_count == 2


class TestRetryBehaviour:
    async def test_two_transient_failures_then_success(self):
        """Tenacity must keep retrying transient errors until either the
        configured attempt cap or a successful response. Without this we'd
        only know retry is wired - not that it completes."""
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ConnectError("transient")
            return ok_response("400.00")

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            assert await oracle.get_tao_usd() == Decimal("400.00")
        assert call_count == 3


class TestConcurrentFetchLock:
    async def test_lock_prevents_thundering_herd(self):
        """N concurrent calls on cold cache should hit CoinGecko exactly once."""
        call_count = 0

        async def slow_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            # Yield control so concurrent tasks can race.
            await asyncio.sleep(0.05)
            return ok_response("400.00")

        async with make_client(slow_handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            results = await asyncio.gather(*(oracle.get_tao_usd() for _ in range(10)))

        assert all(r == Decimal("400.00") for r in results)
        assert call_count == 1


class TestStaleWhileRevalidate:
    async def test_serves_stale_when_oracle_unreachable(self):
        responses: list[httpx.Response | Exception] = [
            ok_response("400.00"),
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        # ttl=0 means second call goes past TTL and tries refetch (which fails).
        config = make_pricing_config(cache_ttl_seconds=0, max_stale_seconds=300)
        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(config, client)
            # First call populates cache.
            assert await oracle.get_tao_usd() == Decimal("400.00")
            # Second call: refetch fails (all 3 retries), stale served.
            assert await oracle.get_tao_usd() == Decimal("400.00")

    async def test_no_cache_no_oracle_raises_unreachable(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            with pytest.raises(OracleUnreachableError):
                await oracle.get_tao_usd()

    async def test_beyond_max_stale_raises_price_too_stale(self):
        """When age exceeds max_stale, refuse to serve."""
        responses: list[httpx.Response | Exception] = [
            ok_response("400.00"),
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
            httpx.ConnectError("boom"),
        ]

        def handler(_request: httpx.Request) -> httpx.Response:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        config = make_pricing_config(cache_ttl_seconds=0, max_stale_seconds=0)
        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(config, client)
            assert await oracle.get_tao_usd() == Decimal("400.00")
            # Force the cache age past max_stale by rewinding fetched_at.
            assert oracle._cache is not None
            price, _ = oracle._cache
            oracle._cache = (price, time.time() - 9999)
            with pytest.raises(PriceTooStaleError):
                await oracle.get_tao_usd()


class TestMalformedPrice:
    @pytest.mark.parametrize("bad", [0, -1, "NaN", "Infinity"])
    async def test_non_positive_or_non_finite_rejected(self, bad: object):
        def handler(_request: httpx.Request) -> httpx.Response:
            return ok_response(bad)  # type: ignore[arg-type]

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            with pytest.raises(MalformedPriceError):
                await oracle.get_tao_usd()

    async def test_missing_usd_field_rejected(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"bittensor": {}})

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            with pytest.raises(MalformedPriceError):
                await oracle.get_tao_usd()

    async def test_missing_bittensor_field_rejected(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            with pytest.raises(MalformedPriceError):
                await oracle.get_tao_usd()

    async def test_non_json_200_body_rejected(self):
        # Simulates CDN-injected error page during an incident: HTTP 200
        # but the body is HTML, not JSON. Without explicit handling the
        # decode raises ValueError and escapes as an unhandled 500.
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<html>nginx 502</html>",
                headers={"content-type": "text/html"},
            )

        async with make_client(handler) as client:
            oracle = CoinGeckoOracle(make_pricing_config(), client)
            with pytest.raises(MalformedPriceError, match="not valid JSON"):
                await oracle.get_tao_usd()
