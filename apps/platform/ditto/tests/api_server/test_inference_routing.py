from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_server.inference_routing import (
    AGGREGATE_PROVIDER,
    ProviderRouteRefresher,
    aggregate_profile_revision,
    rank_routes,
    select_route,
)
from ditto.db.models import (
    InferenceProviderRoute,
    InferenceRoutingPolicy,
)


def _route(
    provider: str,
    *,
    speed: float,
    prompt_price: float,
    completion_price: float,
    errors: float = 0,
    samples: int = 20,
) -> InferenceProviderRoute:
    return InferenceProviderRoute(
        model="openai/gpt-oss-20b",
        provider=provider,
        profile_revision=f"profile-{provider}",
        status="healthy",
        calibration_status="eligible",
        prompt_price_per_token=prompt_price,
        completion_price_per_token=completion_price,
        ewma_tokens_per_second=speed,
        ewma_latency_ms=1000,
        ewma_error_rate=errors,
        ewma_timeout_rate=0,
        calibration_tool_accuracy=0.65,
        calibration_composite=0.20,
        calibration_sample_count=60,
        calibration_manifest_sha256="ab" * 32,
        sample_count=samples,
        discovered_at=datetime.now(UTC),
    )


def test_route_ranking_balances_speed_cost_and_reliability() -> None:
    fast = _route(
        "fast", speed=250, prompt_price=0.00000007, completion_price=0.00000015
    )
    cheap = _route(
        "cheap", speed=130, prompt_price=0.00000003, completion_price=0.00000013
    )
    flaky = _route(
        "flaky",
        speed=600,
        prompt_price=0.00000001,
        completion_price=0.00000001,
        errors=0.8,
    )

    ranked = rank_routes(
        [cheap, flaky, fast],
        speed_weight=0.65,
        cost_weight=0.25,
        exploration_weight=0.10,
    )

    assert ranked[0].route.provider == "fast"
    assert ranked[-1].route.provider == "flaky"


def test_route_calibration_contract_is_explicit_and_future_safe() -> None:
    assert benchmark_contract(7).requires_inference_route_calibration is True
    assert benchmark_contract(8).requires_inference_route_calibration is False
    assert benchmark_contract(9).requires_inference_route_calibration is False
    assert benchmark_contract(10).requires_inference_route_calibration is False
    with pytest.raises(ValueError, match="unsupported benchmark version: 11"):
        benchmark_contract(11)


def test_route_ranking_explores_an_eligible_unmeasured_provider() -> None:
    known = _route(
        "known",
        speed=100,
        prompt_price=0.00000003,
        completion_price=0.00000013,
        samples=100,
    )
    new = _route(
        "new", speed=0, prompt_price=0.00000003, completion_price=0.00000013, samples=0
    )
    new.ewma_tokens_per_second = None

    ranked = rank_routes(
        [known, new],
        speed_weight=0.2,
        cost_weight=0.2,
        exploration_weight=0.6,
    )

    assert ranked[0].route.provider == "new"


def test_route_ranking_will_not_trade_tool_quality_for_raw_speed() -> None:
    sound = _route(
        "sound", speed=150, prompt_price=0.00000003, completion_price=0.00000013
    )
    broken = _route(
        "broken", speed=600, prompt_price=0.00000001, completion_price=0.00000001
    )
    broken.calibration_tool_accuracy = 0.05
    broken.calibration_composite = 0.05

    ranked = rank_routes(
        [broken, sound],
        speed_weight=0.65,
        cost_weight=0.25,
        exploration_weight=0.10,
    )

    assert ranked[0].route.provider == "sound"


@pytest.mark.asyncio
async def test_aggregate_selection_uses_only_reviewed_logical_route(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    maker = session_maker
    now = datetime.now(UTC)
    model = "openai/gpt-oss-20b"
    profile = aggregate_profile_revision(model)
    assert profile == "openrouter-route-a471cd87ae7df5b9-v1"
    async with maker() as session, session.begin():
        session.add(
            InferenceRoutingPolicy(
                model=model,
                enabled=False,
                speed_weight=0.65,
                cost_weight=0.25,
                exploration_weight=0.10,
                exploration_ticket_budget=3,
                min_tool_accuracy=0.55,
                min_composite=0.15,
                min_calibration_samples=20,
                max_error_rate=0.25,
                max_timeout_rate=0.15,
                cooldown_seconds=30,
                ewma_alpha=0.20,
                updated_at=now,
            )
        )
        session.add(
            InferenceProviderRoute(
                model=model,
                provider=AGGREGATE_PROVIDER,
                profile_revision=profile,
                status="healthy",
                calibration_status="eligible",
                calibration_manifest_sha256="ab" * 32,
                calibration_tool_accuracy=0.65,
                calibration_composite=0.20,
                calibration_sample_count=60,
                ewma_error_rate=0,
                ewma_timeout_rate=0,
                sample_count=0,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                discovered_at=now,
                updated_at=now,
            )
        )
    async with maker() as session, session.begin():
        selected = await select_route(
            session,
            model=model,
            now=now,
            supported_profiles=(profile,),
            calibration_manifest_sha256="ab" * 32,
            routing_mode="aggregate_throughput",
        )
        assert selected is not None
        assert selected.provider == AGGREGATE_PROVIDER
        assert selected.selected_ticket_count == 1

    async with maker() as session, session.begin():
        route = await session.get(
            InferenceProviderRoute, (model, AGGREGATE_PROVIDER, profile)
        )
        assert route is not None
        route.calibration_sample_count = 0
        assert (
            await select_route(
                session,
                model=model,
                now=now,
                supported_profiles=(profile,),
                calibration_manifest_sha256="ab" * 32,
                routing_mode="aggregate_throughput",
                bench_version=7,
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("bench_version", [8, 9, 10])
async def test_post_v7_aggregate_selection_ignores_legacy_calibration(
    session_maker: async_sessionmaker[AsyncSession],
    bench_version: int,
) -> None:
    now = datetime.now(UTC)
    model = "openai/gpt-oss-20b"
    v7_profile = aggregate_profile_revision(model, bench_version=7)
    v9_profile = aggregate_profile_revision(model, bench_version=9)
    target_profile = aggregate_profile_revision(model, bench_version=bench_version)
    assert v7_profile == "openrouter-route-a471cd87ae7df5b9-v1"
    assert v9_profile == "openrouter-route-6a097486af3c178d-v1"
    assert v9_profile != v7_profile
    if bench_version == 10:
        assert target_profile == v9_profile

    async with session_maker() as session, session.begin():
        session.add(
            InferenceRoutingPolicy(
                model=model,
                enabled=False,
                speed_weight=0.65,
                cost_weight=0.25,
                exploration_weight=0.10,
                exploration_ticket_budget=3,
                min_tool_accuracy=0.55,
                min_composite=0.15,
                min_calibration_samples=20,
                max_error_rate=0.25,
                max_timeout_rate=0.15,
                cooldown_seconds=30,
                ewma_alpha=0.20,
                updated_at=now,
            )
        )
        session.add(
            InferenceProviderRoute(
                model=model,
                provider=AGGREGATE_PROVIDER,
                profile_revision=target_profile,
                status="discovered",
                calibration_status="shadow",
                calibration_manifest_sha256=None,
                calibration_tool_accuracy=None,
                calibration_composite=None,
                calibration_sample_count=0,
                ewma_error_rate=0,
                ewma_timeout_rate=0,
                sample_count=0,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                discovered_at=now,
                updated_at=now,
            )
        )

    async with session_maker() as session, session.begin():
        selected = await select_route(
            session,
            model=model,
            now=now,
            # These are retired calibration claims the validator may still
            # advertise. They must not gate the v8/v9 operational route.
            supported_profiles=("retired-calibration-profile",),
            calibration_manifest_sha256="ab" * 32,
            routing_mode="aggregate_throughput",
            bench_version=bench_version,
        )
        assert selected is not None
        assert selected.profile_revision == target_profile

    async with session_maker() as session, session.begin():
        route = await session.get(
            InferenceProviderRoute, (model, AGGREGATE_PROVIDER, target_profile)
        )
        assert route is not None
        route.cooldown_until = now + timedelta(minutes=1)
        assert (
            await select_route(
                session,
                model=model,
                now=now,
                supported_profiles=("retired-calibration-profile",),
                calibration_manifest_sha256="ab" * 32,
                routing_mode="aggregate_throughput",
                bench_version=bench_version,
            )
            is None
        )


@pytest.mark.asyncio
async def test_v9_adaptive_selection_ranks_operational_routes_without_calibration(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    model = "openai/gpt-oss-20b"
    async with session_maker() as session, session.begin():
        session.add(
            InferenceRoutingPolicy(
                model=model,
                enabled=True,
                speed_weight=0.65,
                cost_weight=0.25,
                exploration_weight=0.10,
                exploration_ticket_budget=3,
                min_tool_accuracy=0.55,
                min_composite=0.15,
                min_calibration_samples=60,
                max_error_rate=0.25,
                max_timeout_rate=0.15,
                cooldown_seconds=30,
                ewma_alpha=0.20,
                updated_at=now,
            )
        )
        for provider, speed in (("fast", 250.0), ("slow", 50.0)):
            route = _route(
                provider,
                speed=speed,
                prompt_price=0.00000003,
                completion_price=0.00000013,
                samples=100,
            )
            route.calibration_status = "shadow"
            route.calibration_manifest_sha256 = None
            route.calibration_tool_accuracy = None
            route.calibration_composite = None
            route.calibration_sample_count = 0
            route.exploration_ticket_count = 3
            route.selected_ticket_count = 0
            route.updated_at = now
            session.add(route)

    async with session_maker() as session, session.begin():
        selected = await select_route(
            session,
            model=model,
            now=now,
            supported_profiles=("retired-v7-profile",),
            calibration_manifest_sha256="ab" * 32,
            routing_mode="adaptive",
            bench_version=9,
        )
        assert selected is not None
        assert selected.provider == "fast"


@pytest.mark.asyncio
async def test_aggregate_discovery_tracks_active_model_endpoints(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    maker = session_maker
    model = "openai/gpt-oss-20b"
    profile = aggregate_profile_revision(model)
    endpoint_statuses = [1, 0, 1]

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "endpoints": [
                        {
                            "provider_name": "discovered-provider",
                            "quantization": "fp8",
                            "status": endpoint_statuses.pop(0),
                        }
                    ]
                }
            },
        )

    config = SimpleNamespace(
        allowed_models=(model,),
        discovery_url_template="https://openrouter.test/{model}",
        route_speed_weight=0.65,
        route_cost_weight=0.25,
        route_exploration_weight=0.10,
        route_exploration_ticket_budget=3,
        route_min_tool_accuracy=0.55,
        route_min_composite=0.15,
        route_min_calibration_samples=60,
        route_max_error_rate=0.25,
        route_max_timeout_rate=0.15,
        route_cooldown_seconds=30,
        route_ewma_alpha=0.20,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        refresher = ProviderRouteRefresher(
            config=config,
            session_maker=maker,
            client=client,
        )
        for expected in ("offline", "discovered", "offline"):
            await refresher.refresh()
            async with maker() as session:
                for discovered_profile in (
                    profile,
                    aggregate_profile_revision(model, bench_version=9),
                ):
                    route = await session.get(
                        InferenceProviderRoute,
                        (model, AGGREGATE_PROVIDER, discovered_profile),
                    )
                    assert route is not None
                    assert route.status == expected
