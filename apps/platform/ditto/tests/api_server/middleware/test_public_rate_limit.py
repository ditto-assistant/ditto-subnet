"""PublicRateLimitMiddleware buckets, client keying, and route scope."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from prometheus_client import REGISTRY
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ditto.api_server.middleware.error_envelope import ERROR_CODE_RATE_LIMITED
from ditto.api_server.middleware.public_cache import PublicCacheMiddleware
from ditto.api_server.middleware.public_rate_limit import (
    PublicRateLimitMiddleware,
    TokenBuckets,
    rate_limit_key,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _refusals(route: str) -> float:
    return (
        REGISTRY.get_sample_value("ditto_public_rate_limited_total", {"route": route})
        or 0.0
    )


def _build(clock: _Clock, *, per_minute: int = 2, cached: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/upload/eval-pricing")
    async def pricing(request: Request) -> dict:
        return {"client": request.client.host if request.client else None}

    app.state.uploads = 0

    @app.post("/api/v1/upload/agent")
    async def upload(request: Request) -> dict:
        request.app.state.uploads += 1
        return {"bytes": len(await request.body())}

    @app.get("/api/v1/public/cached")
    async def public_cached(response: Response) -> dict:
        response.headers["Cache-Control"] = "public, max-age=60"
        return {}

    @app.get("/api/v1/validator/job")
    async def validator_job() -> dict:
        return {}

    @app.get("/api/v1/admin/leaderboard")
    async def admin_leaderboard() -> dict:
        return {}

    app.add_middleware(PublicRateLimitMiddleware, per_minute=per_minute, now=clock)
    if cached:
        app.add_middleware(PublicCacheMiddleware, now=clock, disabled=False)
    return app


def _client(app, peer: str = "198.51.100.7") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 40000)),
        base_url="http://test",
    )


class TestTokenBuckets:
    def test_burst_then_wait_then_refill(self) -> None:
        clock = _Clock()
        buckets = TokenBuckets(60, now=clock)
        assert all(buckets.acquire("a") == 0.0 for _ in range(60))
        assert buckets.acquire("a") == pytest.approx(1.0)
        assert buckets.acquire("b") == 0.0
        clock.value += 1.0
        assert buckets.acquire("a") == 0.0
        assert buckets.acquire("a") == pytest.approx(1.0)

    def test_idle_buckets_are_evicted_once_full(self) -> None:
        clock = _Clock()
        buckets = TokenBuckets(2, now=clock)
        for key in ("a", "b", "c"):
            buckets.acquire(key)
        clock.value += 30.0
        buckets.acquire("a")
        assert len(buckets) == 3
        clock.value += 30.0
        buckets.acquire("a")
        # b and c went a full minute untouched; a was refreshed.
        assert len(buckets) == 1

    def test_client_count_is_capped(self) -> None:
        buckets = TokenBuckets(1, now=_Clock(), max_clients=3)
        for key in ("a", "b", "c", "d", "e"):
            buckets.acquire(key)
        assert len(buckets) == 3
        # The oldest client was dropped, so it starts over with a full bucket.
        assert buckets.acquire("a") == 0.0


class TestRateLimitKey:
    def test_ipv6_shares_its_slash_64(self) -> None:
        assert (
            rate_limit_key("2001:db8:1:2::1")
            == rate_limit_key("2001:db8:1:2:ffff::9")
            == "2001:db8:1:2::/64"
        )
        assert rate_limit_key("2001:db8:1:3::1") != rate_limit_key("2001:db8:1:2::1")

    def test_ipv4_and_mapped_ipv4_key_per_address(self) -> None:
        assert rate_limit_key("203.0.113.5") == "203.0.113.5"
        assert rate_limit_key("::ffff:203.0.113.5") == "203.0.113.5"

    def test_non_ip_peer_is_its_own_key(self) -> None:
        assert rate_limit_key("testclient") == "testclient"
        assert rate_limit_key(None) == "-"


class TestPublicRateLimitMiddleware:
    async def test_limit_answers_429_in_the_error_envelope(self) -> None:
        clock = _Clock()
        before = _refusals("upload")
        async with _client(_build(clock)) as client:
            for _ in range(2):
                assert (
                    await client.get("/api/v1/upload/eval-pricing")
                ).status_code == 200
            refused = await client.get("/api/v1/upload/eval-pricing")
            assert refused.status_code == 429
            assert refused.headers["Retry-After"] == "30"
            body = refused.json()
            assert body["error_code"] == ERROR_CODE_RATE_LIMITED
            assert set(body) == {"error_code", "message", "request_id"}
            assert _refusals("upload") == before + 1

            clock.value += 30.0
            assert (await client.get("/api/v1/upload/eval-pricing")).status_code == 200

    async def test_upload_refused_without_reaching_the_endpoint(self) -> None:
        app = _build(_Clock(), per_minute=1)
        async with _client(app) as client:
            files = {"agent_tar": ("a.tar.gz", b"x" * 1024)}
            ok = await client.post("/api/v1/upload/agent", files=files)
            assert ok.status_code == 200
            refused = await client.post("/api/v1/upload/agent", files=files)
            assert refused.status_code == 429
        assert app.state.uploads == 1

    async def test_clients_have_separate_buckets(self) -> None:
        app = _build(_Clock(), per_minute=1)
        async with (
            _client(app, "198.51.100.7") as first,
            _client(app, "198.51.100.8") as second,
        ):
            assert (await first.get("/api/v1/upload/eval-pricing")).status_code == 200
            assert (await first.get("/api/v1/upload/eval-pricing")).status_code == 429
            assert (await second.get("/api/v1/upload/eval-pricing")).status_code == 200

    async def test_authenticated_routes_are_never_limited(self) -> None:
        async with _client(_build(_Clock(), per_minute=1)) as client:
            for _ in range(5):
                assert (await client.get("/api/v1/validator/job")).status_code == 200
                assert (
                    await client.get("/api/v1/admin/leaderboard")
                ).status_code == 200

    async def test_public_cache_hits_do_not_spend_the_budget(self) -> None:
        async with _client(_build(_Clock(), per_minute=1, cached=True)) as client:
            for _ in range(5):
                response = await client.get("/api/v1/public/cached")
                assert response.status_code == 200
            # A cache-busting query string reaches the database, so it counts.
            busted = await client.get("/api/v1/public/cached?bust=1")
            assert busted.status_code == 429


class TestForwardedClient:
    """The limiter keys on the peer as uvicorn's proxy-headers support leaves it."""

    @staticmethod
    def _behind_uvicorn(peer: str) -> httpx.AsyncClient:
        app = ProxyHeadersMiddleware(
            _build(_Clock(), per_minute=1),  # type: ignore[arg-type]
            trusted_hosts="127.0.0.1",
        )
        return _client(app, peer)

    async def test_spoofed_header_from_untrusted_peer_is_ignored(self) -> None:
        async with self._behind_uvicorn("198.51.100.7") as client:
            first = await client.get(
                "/api/v1/upload/eval-pricing",
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            assert first.json() == {"client": "198.51.100.7"}
            rotated = await client.get(
                "/api/v1/upload/eval-pricing",
                headers={"X-Forwarded-For": "203.0.113.2"},
            )
            assert rotated.status_code == 429

    async def test_trusted_proxy_hop_is_honored(self) -> None:
        async with self._behind_uvicorn("127.0.0.1") as proxy:
            # The caller wrote the left-most entry; the proxy appended the right.
            first = await proxy.get(
                "/api/v1/upload/eval-pricing",
                headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.1"},
            )
            assert first.json() == {"client": "203.0.113.1"}
            spoofed = await proxy.get(
                "/api/v1/upload/eval-pricing",
                headers={"X-Forwarded-For": "10.9.9.8, 203.0.113.1"},
            )
            assert spoofed.status_code == 429
            other = await proxy.get(
                "/api/v1/upload/eval-pricing",
                headers={"X-Forwarded-For": "203.0.113.2"},
            )
            assert other.status_code == 200
