"""Unit tests for the bounded per-component stack-health probes (heartbeat v9).

The collector is telemetry: every failure mode must degrade to a truthful
state (``unreachable``/``degraded``/``unknown``) without raising, without
stalling, and without leaking anything host-shaped into the public payload.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ValidatorComponentIdentity,
    ValidatorStackComponents,
    ValidatorStackIdentity,
)
from ditto.validator.stack_health import (
    StackHealthCollector,
    fallback_stack_health,
)

_REV = "ab" * 20
_OTHER_REV = "cd" * 20

_SANDBOX_URL = "http://sandbox-docker.internal:2375/_ping"
_RELAY_URL = "http://model-relay.internal:8080/healthz"
_PYLON_URL = "http://pylon.internal:8000"
_OLLAMA_URL = "http://sandbox-docker.internal:11434/api/embed"


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "dittobench_mock": False,
        "sandbox_docker_probe_url": _SANDBOX_URL,
        "model_relay_probe_url": _RELAY_URL,
        "pylon_probe_url": "",
        "pylon_url": _PYLON_URL,
        "embed_preflight_url": _OLLAMA_URL,
        "stack_probe_timeout_seconds": 2.0,
        "stack_health_cache_seconds": 60.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _component(revision: str | None = _REV) -> ValidatorComponentIdentity:
    return ValidatorComponentIdentity(
        source_revision=revision,
        version="unknown" if revision is None else None,
        provenance="committed_pin" if revision else "local_unverified",
    )


def _stack() -> ValidatorStackIdentity:
    return ValidatorStackIdentity(
        mode="source",
        compose_schema=1,
        release_descriptor_digest=None,
        components=ValidatorStackComponents(
            ditto_subnet=_component(),
            dittobench_api=_component(),
            sandbox_docker=_component(None),
            model_relay=_component(),
            pylon=_component(None),
            ollama=_component(None),
        ),
    )


def _fresh_scorer() -> ScorerBenchmarkCapability:
    return ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 3),
        observed_at=1_784_020_800,
        software_version="1.2.3",
        source_revision=_REV,
    )


def _client(handler) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _all_up_handler(
    relay_body: dict[str, object] | None = None,
) -> object:
    body = (
        relay_body
        if relay_body is not None
        else {
            "status": "ok",
            "model_route_ready": True,
            "source_revision": _REV,
            "version": "1.2.3",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == _SANDBOX_URL:
            return httpx.Response(200, text="OK")
        if url == _RELAY_URL:
            return httpx.Response(200, json=body)
        if url.startswith(_PYLON_URL):
            return httpx.Response(404, json={"detail": "not found"})
        if url == _OLLAMA_URL:
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
        raise AssertionError(f"unexpected probe URL {url}")

    return handler


class TestCollector:
    async def test_full_stack_healthy(self) -> None:
        async with _client(_all_up_handler()) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.ditto_subnet.health == "healthy"
        assert health.ditto_subnet.observed_identity is not None
        assert health.dittobench_api.health == "healthy"
        assert health.dittobench_api.observed_identity is not None
        assert health.dittobench_api.observed_identity.source_revision == _REV
        assert health.sandbox_docker.health == "healthy"
        assert health.model_relay.health == "healthy"
        assert health.model_relay.model_ready is True
        assert health.model_relay.observed_identity is not None
        # An unauthenticated 404 still proves the Pylon API is up and serving.
        assert health.pylon.health == "healthy"
        assert health.ollama.health == "healthy"
        assert health.ollama.model_ready is True

    async def test_unreachable_sidecars(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        for name in ("sandbox_docker", "model_relay", "pylon", "ollama"):
            component = getattr(health, name)
            assert component.health == "unreachable"
            assert component.observed_at is not None
            assert component.observed_identity is None
        # The reporting process and the (separately observed) scorer stand.
        assert health.ditto_subnet.health == "healthy"
        assert health.dittobench_api.health == "healthy"

    async def test_timeout_is_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())
        assert health.ollama.health == "unreachable"

    async def test_degraded_states(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url == _SANDBOX_URL:
                return httpx.Response(500, text="boom")
            if url == _RELAY_URL:
                return httpx.Response(503, text="starting")
            if url.startswith(_PYLON_URL):
                return httpx.Response(502, text="bad gateway")
            if url == _OLLAMA_URL:
                # Reachable, but the required embedding model is not loaded.
                return httpx.Response(200, json={"embeddings": []})
            raise AssertionError(url)

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.sandbox_docker.health == "degraded"
        assert health.model_relay.health == "degraded"
        assert health.pylon.health == "degraded"
        assert health.ollama.health == "degraded"
        assert health.ollama.model_ready is False

    async def test_model_relay_identity_mismatch(self) -> None:
        handler = _all_up_handler(
            relay_body={
                "status": "ok",
                "model_route_ready": True,
                "source_revision": _OTHER_REV,
            }
        )
        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.model_relay.health == "identity_mismatch"
        assert health.model_relay.observed_identity is not None
        assert health.model_relay.observed_identity.source_revision == _OTHER_REV

    async def test_relay_never_copies_configured_pin(self) -> None:
        # The relay answers healthy but reports no identity: the observed
        # identity must stay unknown, not echo the committed pin.
        handler = _all_up_handler(relay_body={"status": "ok"})
        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.model_relay.health == "healthy"
        assert health.model_relay.observed_identity is None
        assert health.model_relay.model_ready is None

    async def test_relay_without_health_route_still_proves_reachability(self) -> None:
        # The relay may not serve the probed path at all; a 404 still proves
        # the process is up, and no observation is invented from its body.
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == _RELAY_URL:
                return httpx.Response(404, text="not found")
            return _all_up_handler()(request)  # type: ignore[operator]

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.model_relay.health == "healthy"
        assert health.model_relay.ready is True
        assert health.model_relay.observed_identity is None

    async def test_unconfigured_probes_stay_unknown(self) -> None:
        config = _config(
            sandbox_docker_probe_url="",
            model_relay_probe_url="",
            embed_preflight_url="",
            pylon_probe_url="",
            pylon_url="",
        )

        async with _client(_all_up_handler()) as client:
            collector = StackHealthCollector(config, client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        for name in ("sandbox_docker", "model_relay", "pylon", "ollama"):
            assert getattr(health, name).health == "unknown"

    async def test_mock_mode_performs_no_probes(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("mock mode must not touch the network")

        async with _client(handler) as client:
            config = _config(dittobench_mock=True)
            collector = StackHealthCollector(config, client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        assert health.ditto_subnet.health == "healthy"
        assert health.dittobench_api.health == "unknown"
        assert health.ollama.health == "unknown"

    async def test_sidecar_snapshot_is_cached_within_ttl(self) -> None:
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return _all_up_handler()(request)  # type: ignore[operator]

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            await collector.collect(stack=_stack(), scorer=_fresh_scorer())
            first = calls["count"]
            await collector.collect(stack=_stack(), scorer=_fresh_scorer())
        assert first == 4
        assert calls["count"] == 4

    async def test_zero_cache_reprobes_every_collect(self) -> None:
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return _all_up_handler()(request)  # type: ignore[operator]

        async with _client(handler) as client:
            config = _config(stack_health_cache_seconds=0.0)
            collector = StackHealthCollector(config, client)  # type: ignore[arg-type]
            await collector.collect(stack=_stack(), scorer=_fresh_scorer())
            await collector.collect(stack=_stack(), scorer=_fresh_scorer())
        assert calls["count"] == 8

    async def test_probe_sweep_crash_degrades_to_unknown(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise RuntimeError("transport exploded")

        async with _client(handler) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        for name in ("sandbox_docker", "model_relay", "pylon", "ollama"):
            assert getattr(health, name).health == "unknown"
        assert health.ditto_subnet.health == "healthy"

    async def test_payload_never_contains_probe_urls(self) -> None:
        async with _client(_all_up_handler()) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=_fresh_scorer())

        payload = json.dumps(health.model_dump(mode="json"))
        for needle in ("://", "internal", "2375", "11434", "8080"):
            assert needle not in payload


class TestScorerMapping:
    @pytest.mark.parametrize(
        ("scorer", "expected"),
        [
            (_fresh_scorer(), "healthy"),
            (
                ScorerBenchmarkCapability(
                    status="legacy_v2", supported_bench_versions=(2,)
                ),
                "degraded",
            ),
            (
                ScorerBenchmarkCapability(
                    status="unreachable", supported_bench_versions=(2,)
                ),
                "unreachable",
            ),
            (
                ScorerBenchmarkCapability(
                    status="identity_mismatch",
                    supported_bench_versions=(2,),
                    observed_at=1_784_020_800,
                    software_version="1.2.3",
                    source_revision=_OTHER_REV,
                ),
                "identity_mismatch",
            ),
        ],
    )
    async def test_scorer_status_maps_to_component_health(
        self, scorer: ScorerBenchmarkCapability, expected: str
    ) -> None:
        async with _client(_all_up_handler()) as client:
            collector = StackHealthCollector(_config(), client)  # type: ignore[arg-type]
            health = await collector.collect(stack=_stack(), scorer=scorer)
        assert health.dittobench_api.health == expected


class TestFallback:
    def test_fallback_claims_only_the_reporter(self) -> None:
        health = fallback_stack_health()
        assert health.ditto_subnet.health == "healthy"
        assert health.ditto_subnet.ready is True
        for name in (
            "dittobench_api",
            "sandbox_docker",
            "model_relay",
            "pylon",
            "ollama",
        ):
            assert getattr(health, name).health == "unknown"
