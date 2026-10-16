"""PublicCacheMiddleware TTL, single-flight, and scope coverage."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from ditto.api_server.middleware.public_cache import (
    PublicCacheMiddleware,
    _policy_from_cache_control,
)
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


_PINNED_ETAG = '"' + "ab" * 32 + '"'
_WEAK_ETAG = 'W/"' + "cd" * 16 + '"'


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
        "pinned": 0,
        "weak": 0,
        "pinned_big": 0,
        "split": 0,
        "validating": 0,
        "uncacheable": 0,
        "no_cache": 0,
        "split_fields": 0,
        "uncacheable_big": 0,
    }
    seen_validators: list[str | None] = []

    @app.get("/api/v1/public/pinned")
    async def pinned() -> Response:
        # Content-addressed like the pinned CLA text: the endpoint owns its
        # digest validator and hardening headers.
        calls["pinned"] += 1
        return Response(
            content=b"TEST FIXTURE ONLY",
            media_type="text/plain",
            headers={
                "ETag": _PINNED_ETAG,
                "Cache-Control": "public, max-age=10",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/v1/public/pinned-big")
    async def pinned_big() -> Response:
        # Large enough to gzip, like real agreement text behind a digest ETag.
        calls["pinned_big"] += 1
        return Response(
            content=b"TEST FIXTURE ONLY. " * 300,
            media_type="text/plain",
            headers={"ETag": _PINNED_ETAG, "Cache-Control": "public, max-age=10"},
        )

    @app.get("/api/v1/public/split-cache-control")
    async def split_cache_control() -> Response:
        calls["split"] += 1
        response = Response(content=b"TEST FIXTURE ONLY", media_type="text/plain")
        response.raw_headers.extend(
            [
                (b"cache-control", b"public, max-age=10"),
                (b"cache-control", b"no-store"),
            ]
        )
        return response

    @app.get("/api/v1/public/validating")
    async def validating(request: Request) -> Response:
        # Answers its own conditional requests, like the CLA text endpoint.
        calls["validating"] += 1
        validator = request.headers.get("if-none-match")
        seen_validators.append(validator)
        headers = {"ETag": _WEAK_ETAG, "Cache-Control": "public, max-age=10"}
        if validator is not None:
            return Response(status_code=304, headers={"ETag": '"stale-strong"'})
        return Response(b"TEST FIXTURE ONLY", media_type="text/plain", headers=headers)

    @app.get("/api/v1/public/uncacheable")
    async def uncacheable() -> Response:
        calls["uncacheable"] += 1
        return Response(
            b"TEST FIXTURE ONLY",
            media_type="text/plain",
            headers={"ETag": _PINNED_ETAG, "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/public/uncacheable-big")
    async def uncacheable_big() -> Response:
        # Large enough to gzip, but not cacheable: served through the cache.
        calls["uncacheable_big"] += 1
        return Response(
            content=b"TEST FIXTURE ONLY. " * 300,
            media_type="text/plain",
            headers={"ETag": _PINNED_ETAG, "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/public/no-cache")
    async def no_cache() -> Response:
        calls["no_cache"] += 1
        return Response(
            b"TEST FIXTURE ONLY",
            media_type="text/plain",
            headers={"Cache-Control": "no-cache, max-age=10"},
        )

    @app.get("/api/v1/public/split-fields")
    async def split_fields() -> Response:
        calls["split_fields"] += 1
        response = Response(b"TEST FIXTURE ONLY", media_type="text/plain")
        response.raw_headers.extend(
            [
                (b"cache-control", b"public, max-age=10"),
                (b"cache-control", b"immutable"),
            ]
        )
        return response

    @app.get("/api/v1/public/weak")
    async def weak() -> Response:
        calls["weak"] += 1
        return Response(
            content=b"TEST FIXTURE ONLY",
            media_type="text/plain",
            headers={"ETag": _WEAK_ETAG, "Cache-Control": "public, max-age=10"},
        )

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
    app.state.seen_validators = seen_validators
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


async def test_endpoint_validator_and_hardening_headers_survive_the_cache(
    clock: _Clock,
) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        miss = await client.get("/api/v1/public/pinned")
        hit = await client.get("/api/v1/public/pinned")
        revalidated = await client.get(
            "/api/v1/public/pinned", headers={"If-None-Match": _PINNED_ETAG}
        )
    assert (miss.headers["X-Public-Cache"], hit.headers["X-Public-Cache"]) == (
        "MISS",
        "HIT",
    )
    for response in (miss, hit):
        assert response.headers["etag"] == _PINNED_ETAG
        assert response.headers["x-content-type-options"] == "nosniff"
    # A client that pinned the published digest revalidates from the cache.
    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == _PINNED_ETAG
    assert calls["pinned"] == 1


async def test_declared_weak_etag_revalidates_with_either_form(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        miss = await client.get(
            "/api/v1/public/weak", headers={"If-None-Match": _WEAK_ETAG}
        )
        echoed = await client.get(
            "/api/v1/public/weak", headers={"If-None-Match": _WEAK_ETAG}
        )
        strong = await client.get(
            "/api/v1/public/weak", headers={"If-None-Match": _WEAK_ETAG[2:]}
        )
    # A client echoing the endpoint's own weak validator must get a 304.
    for response in (miss, echoed, strong):
        assert response.status_code == 304
        assert response.headers["etag"] == _WEAK_ETAG
    assert calls["weak"] == 1


async def test_cached_gzip_entry_weakens_a_declared_etag_and_still_revalidates(
    clock: _Clock,
) -> None:
    app, calls = _build(clock, gzip=True)
    gzip = {"Accept-Encoding": "gzip"}
    identity = {"Accept-Encoding": "identity"}
    async with _client(app) as client:
        compressed = await client.get("/api/v1/public/pinned-big", headers=gzip)
        plain = await client.get("/api/v1/public/pinned-big", headers=identity)
        revalidated = {
            name: await client.get(
                "/api/v1/public/pinned-big",
                headers={**headers, "If-None-Match": _PINNED_ETAG},
            )
            for name, headers in (("gzip", gzip), ("identity", identity))
        }
    # Distinct bytes never share a strong validator.
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.headers["etag"] == "W/" + _PINNED_ETAG
    assert "content-encoding" not in plain.headers
    assert plain.headers["etag"] == _PINNED_ETAG
    # A client pinning the published digest revalidates either variant.
    assert revalidated["gzip"].status_code == 304
    assert revalidated["gzip"].headers["etag"] == "W/" + _PINNED_ETAG
    assert revalidated["identity"].status_code == 304
    assert revalidated["identity"].headers["etag"] == _PINNED_ETAG
    assert calls["pinned_big"] == 2


async def test_no_store_in_any_cache_control_field_prevents_caching(
    clock: _Clock,
) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        for _ in range(2):
            response = await client.get("/api/v1/public/split-cache-control")
            assert response.status_code == 200
            assert response.headers.get("x-public-cache") != "HIT"
    assert calls["split"] == 2


async def test_cold_conditional_request_is_answered_and_stored_by_the_cache(
    clock: _Clock,
) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        cold = await client.get(
            "/api/v1/public/validating", headers={"If-None-Match": _WEAK_ETAG[2:]}
        )
        warm = await client.get(
            "/api/v1/public/validating", headers={"If-None-Match": _WEAK_ETAG}
        )
        full = await client.get("/api/v1/public/validating")
    # The endpoint never sees the validator, so it cannot answer its own 304
    # with a different validator, and the cold request fills the cache.
    assert app.state.seen_validators == [None]
    assert calls["validating"] == 1
    for response in (cold, warm):
        assert response.status_code == 304
        assert response.headers["etag"] == _WEAK_ETAG
        assert response.headers["vary"] == "Accept-Encoding"
    assert full.status_code == 200 and full.headers["x-public-cache"] == "HIT"


async def test_uncacheable_response_still_answers_a_matching_validator(
    clock: _Clock,
) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        matched = await client.get(
            "/api/v1/public/uncacheable", headers={"If-None-Match": _PINNED_ETAG}
        )
        missed = await client.get(
            "/api/v1/public/uncacheable", headers={"If-None-Match": '"other"'}
        )
    assert matched.status_code == 304 and matched.content == b""
    assert matched.headers["etag"] == _PINNED_ETAG
    assert matched.headers["cache-control"] == "no-store"
    assert missed.status_code == 200 and missed.content == b"TEST FIXTURE ONLY"
    assert calls["uncacheable"] == 2


async def test_no_cache_responses_are_never_stored(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        for _ in range(2):
            response = await client.get("/api/v1/public/no-cache")
            assert response.headers.get("x-public-cache") != "HIT"
    assert calls["no_cache"] == 2


@pytest.mark.parametrize(
    ("header", "policy"),
    [
        ("public, max-age=15, stale-while-revalidate=60", (15.0, 60.0)),
        ("Public, Max-Age=30", (30.0, 0.0)),
        # An extension directive is neither a refusal nor a lifetime.
        ("public, max-age=60, x-no-cache-hint=1", (60.0, 0.0)),
        ("public, x-private-hint, max-age=5", (5.0, 0.0)),
        ("public, x-max-age=60", (0.0, 0.0)),
        ("no-cache, max-age=10", (0.0, 0.0)),
        ('private="set-cookie", max-age=10', (0.0, 0.0)),
        ("public, no-store", (0.0, 0.0)),
    ],
)
def test_cache_control_directives_match_by_name(
    header: str, policy: tuple[float, float]
) -> None:
    assert _policy_from_cache_control(header) == policy


async def test_split_header_fields_are_stored_whole(clock: _Clock) -> None:
    app, calls = _build(clock)
    async with _client(app) as client:
        await client.get("/api/v1/public/split-fields")
        hit = await client.get("/api/v1/public/split-fields")
    assert hit.headers["x-public-cache"] == "HIT"
    assert hit.headers["cache-control"] == "public, max-age=10, immutable"
    assert calls["split_fields"] == 1


async def test_uncacheable_gzip_response_gets_the_same_weak_validator(
    clock: _Clock,
) -> None:
    app, calls = _build(clock, gzip=True)
    gzip = {"Accept-Encoding": "gzip"}
    async with _client(app) as client:
        full = await client.get("/api/v1/public/uncacheable-big", headers=gzip)
        revalidated = await client.get(
            "/api/v1/public/uncacheable-big",
            headers={**gzip, "If-None-Match": _PINNED_ETAG},
        )
    assert full.headers["content-encoding"] == "gzip"
    # The stored and the pass-through paths apply one rule to encoded bytes.
    assert full.headers["etag"] == "W/" + _PINNED_ETAG
    assert revalidated.status_code == 304
    assert revalidated.headers["etag"] == "W/" + _PINNED_ETAG
    assert calls["uncacheable_big"] == 2
