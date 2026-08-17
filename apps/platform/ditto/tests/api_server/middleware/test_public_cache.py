"""PublicCacheMiddleware TTL, single-flight, and scope coverage."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Response

from ditto.api_server.middleware.public_cache import PublicCacheMiddleware
from ditto.api_server.middleware.sized_gzip import SizedGZipMiddleware


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class _RefreshGate:
    """Hold a background refresh until the test releases it.

    Wall-clock sleeps made stale-while-revalidate assertions flake under CI
    load (the 40ms "immediate" bound lost to scheduling). The gate makes
    "returned before refresh finished" a happens-before, not a timeout.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.done = asyncio.Event()

    async def hold(self) -> None:
        self.started.set()
        await self.release.wait()
        self.done.set()


def _build(
    clock: _Clock,
    *,
    disabled: bool = False,
    gzip: bool = False,
    stale_gate: _RefreshGate | None = None,
    stale_fail_gate: _RefreshGate | None = None,
) -> tuple[FastAPI, dict[str, int]]:
    app = FastAPI()
    calls = {
        "cached": 0,
        "nostore": 0,
        "post": 0,
        "slow": 0,
        "stale": 0,
        "stale_fail": 0,
        "outside": 0,
        "big": 0,
    }

    @app.get("/api/v1/public/cached")
    async def cached(response: Response) -> dict:
        calls["cached"] += 1
        response.headers["Cache-Control"] = "public, max-age=10"
        return {"n": calls["cached"]}

    @app.get("/api/v1/public/big")
    async def big(response: Response) -> dict:
        calls["big"] += 1
        response.headers["Cache-Control"] = "public, max-age=10"
        # Stable body (independent of call count) so its ETag is byte-stable
        # across recomputes; well over the 1KB gzip floor and compressible.
        return {"pad": "x" * 4000}

    @app.get("/api/v1/public/nostore")
    async def nostore(response: Response) -> dict:
        calls["nostore"] += 1
        response.headers["Cache-Control"] = "no-store"
        return {"n": calls["nostore"]}

    @app.post("/api/v1/public/cached")
    async def posted() -> dict:
        calls["post"] += 1
        return {"n": calls["post"]}

    @app.get("/api/v1/public/slow")
    async def slow(response: Response) -> dict:
        calls["slow"] += 1
        await asyncio.sleep(0.05)
        response.headers["Cache-Control"] = "public, max-age=10"
        return {"n": calls["slow"]}

    @app.get("/api/v1/public/stale")
    async def stale(response: Response) -> dict:
        calls["stale"] += 1
        if calls["stale"] > 1 and stale_gate is not None:
            await stale_gate.hold()
        response.headers["Cache-Control"] = (
            "public, max-age=10, stale-while-revalidate=30"
        )
        return {"n": calls["stale"]}

    @app.get("/api/v1/public/stale-fail")
    async def stale_fail(response: Response) -> dict:
        calls["stale_fail"] += 1
        response.headers["Cache-Control"] = (
            "public, max-age=10, stale-while-revalidate=30"
        )
        if calls["stale_fail"] > 1:
            if stale_fail_gate is not None:
                await stale_fail_gate.hold()
            response.status_code = 503
        return {"n": calls["stale_fail"]}

    @app.get("/api/v1/other")
    async def outside(response: Response) -> dict:
        calls["outside"] += 1
        response.headers["Cache-Control"] = "public, max-age=10"
        return {"n": calls["outside"]}

    # The endpoint-test conftest sets PUBLIC_CACHE_DISABLED for the whole
    # process, so these tests pin the flag explicitly instead of reading env.
    if gzip:
        # Mirror production: gzip is inner, then the cache wraps and stores
        # each already-compressed/identity representation independently.
        app.add_middleware(SizedGZipMiddleware, minimum_size=1000, compresslevel=6)
    app.add_middleware(PublicCacheMiddleware, now=clock, disabled=disabled)
    return app, calls


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_fresh_hits_share_one_downstream_call(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        first = await client.get("/api/v1/public/cached")
        second = await client.get("/api/v1/public/cached")
    assert first.json() == second.json() == {"n": 1}
    assert first.headers["X-Public-Cache"] == "MISS"
    assert second.headers["X-Public-Cache"] == "HIT"
    assert calls["cached"] == 1


async def test_entry_expires_after_declared_max_age(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        await client.get("/api/v1/public/cached")
        clock.value += 11
        refreshed = await client.get("/api/v1/public/cached")
    assert refreshed.json() == {"n": 2}
    assert calls["cached"] == 2


async def test_stale_hit_returns_immediately_while_one_refresh_runs(
    clock: _Clock,
) -> None:
    gate = _RefreshGate()
    app, calls = _build(clock, stale_gate=gate)
    async with _client(app) as client:
        first = await client.get("/api/v1/public/stale")
        clock.value += 11
        stale_responses = await asyncio.gather(
            *(client.get("/api/v1/public/stale") for _ in range(6))
        )
        assert first.json() == {"n": 1}
        assert {response.json()["n"] for response in stale_responses} == {1}
        assert {response.headers["X-Public-Cache"] for response in stale_responses} == {
            "STALE"
        }
        # The six pollers already returned. The refresh is still parked on the
        # gate, so this is a happens-before, not a 40ms race against CI load.
        await asyncio.wait_for(gate.started.wait(), timeout=1)
        assert not gate.done.is_set()
        gate.release.set()
        await asyncio.wait_for(gate.done.wait(), timeout=1)
        refreshed = await client.get("/api/v1/public/stale")
    assert refreshed.json() == {"n": 2}
    assert refreshed.headers["X-Public-Cache"] == "HIT"
    assert calls["stale"] == 2


async def test_failed_background_refresh_backs_off_incoming_pollers(
    clock: _Clock,
) -> None:
    gate = _RefreshGate()
    app, calls = _build(clock, stale_fail_gate=gate)
    async with _client(app) as client:
        seeded = await client.get("/api/v1/public/stale-fail")
        clock.value += 11
        stale = await client.get("/api/v1/public/stale-fail")
        await asyncio.wait_for(gate.started.wait(), timeout=1)
        gate.release.set()
        await asyncio.wait_for(gate.done.wait(), timeout=1)
        retries = await asyncio.gather(
            *(client.get("/api/v1/public/stale-fail") for _ in range(6))
        )
    assert seeded.json() == stale.json() == {"n": 1}
    assert stale.headers["X-Public-Cache"] == "STALE"
    assert {response.json()["n"] for response in retries} == {1}
    assert calls["stale_fail"] == 2


async def test_no_store_post_and_non_public_paths_bypass(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        for _ in range(2):
            await client.get("/api/v1/public/nostore")
            await client.post("/api/v1/public/cached")
            await client.get("/api/v1/other")
    assert calls["nostore"] == 2
    assert calls["post"] == 2
    assert calls["outside"] == 2


async def test_query_string_is_part_of_the_key(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        await client.get("/api/v1/public/cached?page=1")
        await client.get("/api/v1/public/cached?page=2")
        await client.get("/api/v1/public/cached?page=1")
    assert calls["cached"] == 2


async def test_concurrent_misses_are_single_flighted(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        responses = await asyncio.gather(
            *(client.get("/api/v1/public/slow") for _ in range(8))
        )
    assert calls["slow"] == 1
    assert {r.json()["n"] for r in responses} == {1}


async def test_kill_switch_disables_caching(clock: _Clock) -> None:
    app, calls = _build(clock, disabled=True)
    async with _client(app) as client:
        await client.get("/api/v1/public/cached")
        await client.get("/api/v1/public/cached")
    assert calls["cached"] == 2


async def test_strong_etag_present_on_miss_and_hit(clock: _Clock) -> None:
    app, _ = _build(clock)
    async with _client(app) as client:
        miss = await client.get("/api/v1/public/cached")
        hit = await client.get("/api/v1/public/cached")
    assert miss.headers["X-Public-Cache"] == "MISS"
    assert hit.headers["X-Public-Cache"] == "HIT"
    etag = miss.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    # Same body across MISS and HIT -> identical strong ETag.
    assert hit.headers["etag"] == etag


async def test_if_none_match_returns_304_on_hit(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        first = await client.get("/api/v1/public/cached")
        etag = first.headers["etag"]
        second = await client.get(
            "/api/v1/public/cached", headers={"If-None-Match": etag}
        )
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["Cache-Control"] == "public, max-age=10"
    assert second.headers["X-Public-Cache"] == "HIT"
    # RFC 9110 §15.4.5: the 304 repeats the 200's Vary: Accept-Encoding.
    assert second.headers["Vary"] == "Accept-Encoding"
    # A 304 served from cache still cost no extra downstream call.
    assert calls["cached"] == 1


async def test_if_none_match_returns_304_on_miss(clock: _Clock) -> None:
    # First populate the cache to learn the ETag, then request a *cold* key
    # (distinct query) whose freshly-computed ETag matches -> 304 on the MISS
    # path. Same body text means the same ETag regardless of query string.
    app, calls = _build(clock)
    async with _client(app) as client:
        seed = await client.get("/api/v1/public/big")
        etag = seed.headers["etag"]
        clock.value += 100  # expire the seeded entry so the next call is a MISS
        miss = await client.get("/api/v1/public/big", headers={"If-None-Match": etag})
    assert seed.headers["X-Public-Cache"] == "MISS"
    assert miss.status_code == 304
    assert miss.content == b""
    assert miss.headers["etag"] == etag
    assert miss.headers["Cache-Control"] == "public, max-age=10"
    assert miss.headers["X-Public-Cache"] == "MISS"
    # The downstream endpoint ran again to recompute the body for revalidation.
    assert calls["big"] == 2


async def test_304_has_no_body_but_keeps_cache_control(clock: _Clock) -> None:
    app, _ = _build(clock)
    async with _client(app) as client:
        first = await client.get("/api/v1/public/cached")
        second = await client.get(
            "/api/v1/public/cached", headers={"If-None-Match": first.headers["etag"]}
        )
    assert second.status_code == 304
    assert second.content == b""
    assert "content-type" not in second.headers
    assert second.headers["Cache-Control"] == "public, max-age=10"


async def test_gzip_wraps_cache_hit(clock: _Clock) -> None:
    app, calls = _build(clock, gzip=True)
    async with _client(app) as client:
        miss = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "gzip"}
        )
        hit = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "gzip"}
        )
    # One downstream call; both responses use the one cached gzip
    # representation, so the HIT performs no endpoint work or recompression.
    assert calls["big"] == 1
    assert miss.headers["X-Public-Cache"] == "MISS"
    assert hit.headers["X-Public-Cache"] == "HIT"
    assert miss.headers["content-encoding"] == "gzip"
    assert hit.headers["content-encoding"] == "gzip"
    # httpx transparently decodes; the ETag is over the uncompressed body.
    assert miss.headers["etag"] == hit.headers["etag"]
    assert miss.json() == hit.json()


async def test_identity_and_gzip_representations_are_cached_separately(
    clock: _Clock,
) -> None:
    app, calls = _build(clock, gzip=True)
    async with _client(app) as client:
        identity_miss = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "identity"}
        )
        identity_hit = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "identity"}
        )
        gzip_miss = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "gzip"}
        )
        gzip_hit = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "gzip"}
        )
    assert calls["big"] == 2
    assert identity_miss.headers["X-Public-Cache"] == "MISS"
    assert identity_hit.headers["X-Public-Cache"] == "HIT"
    assert "content-encoding" not in identity_hit.headers
    assert gzip_miss.headers["X-Public-Cache"] == "MISS"
    assert gzip_hit.headers["X-Public-Cache"] == "HIT"
    assert gzip_hit.headers["content-encoding"] == "gzip"
    assert identity_hit.headers["etag"] != gzip_hit.headers["etag"]


async def test_gzip_skips_small_responses(clock: _Clock) -> None:
    # The cache middleware re-chunks bodies (BaseHTTPMiddleware), which
    # defeats stock GZipMiddleware's first-chunk minimum_size check;
    # SizedGZipMiddleware applies the floor from the declared Content-Length.
    app, _ = _build(clock, gzip=True)
    async with _client(app) as client:
        miss = await client.get(
            "/api/v1/public/cached", headers={"Accept-Encoding": "gzip"}
        )
        hit = await client.get(
            "/api/v1/public/cached", headers={"Accept-Encoding": "gzip"}
        )
    assert miss.headers["X-Public-Cache"] == "MISS"
    assert hit.headers["X-Public-Cache"] == "HIT"
    for resp in (miss, hit):
        assert "content-encoding" not in resp.headers
        assert resp.headers["vary"] == "Accept-Encoding"


async def test_gzip_wrapped_304_carries_vary(clock: _Clock) -> None:
    app, _ = _build(clock, gzip=True)
    async with _client(app) as client:
        first = await client.get(
            "/api/v1/public/big", headers={"Accept-Encoding": "gzip"}
        )
        second = await client.get(
            "/api/v1/public/big",
            headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]},
        )
    assert first.headers["Vary"] == "Accept-Encoding"
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["Vary"] == "Accept-Encoding"
