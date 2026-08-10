"""DB-backed benchmark route admission, selection, and telemetry.

Aggregate mode admits one versioned logical route. V7 through v9 retain their
historical OpenRouter identities. V10 admits a distinct reviewed provider-list
route whose trusted gateways are ordered in Backroom; OpenRouter keeps its own
throughput routing inside that adapter. Adaptive mode retains exact
provider-per-ticket selection behind an explicit flag.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.db.models import (
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
)

logger = logging.getLogger(__name__)
AGGREGATE_PROVIDER = "openrouter"
V10_AGGREGATE_PROVIDER = "provider-list"
AGGREGATE_CALIBRATION_SAMPLES = 60
V7_MODEL = "openai/gpt-oss-20b"
V7_AGGREGATE_PROFILE_REVISION = "openrouter-route-a471cd87ae7df5b9-v1"
V9_AGGREGATE_PROFILE_REVISION = "openrouter-route-6a097486af3c178d-v1"
V10_AGGREGATE_PROFILE_REVISION = "provider-list-route-bf48ee4a39ff8119-v1"
BENCH_V9_DEFAULT_REASONING_EFFORT = "medium"
BENCH_V9_REASONING_EFFORTS = frozenset({"low", "medium", "high"})


def benchmark_reasoning(model: str) -> dict[str, Any] | None:
    """Return the immutable reasoning contract for a benchmark model."""
    if model == V7_MODEL:
        # GPT-OSS reasoning is mandatory. Pin OpenRouter's current default so a
        # provider/default change cannot silently alter benchmark semantics,
        # latency, or token accounting. The trusted proxy strips reasoning
        # details from its public response, so exclude them upstream as well.
        return {"effort": "medium", "exclude": True}
    return None


def aggregate_profile_revision(model: str, *, bench_version: int = 7) -> str:
    """Return the immutable identity for the calibrated aggregate route."""
    # Reasoning is part of benchmark semantics and therefore part of the route
    # identity. V7/V8 pin medium. V9 and later let the agent select
    # low/medium/high with a medium default. V10 additionally changes the
    # trusted gateway boundary, so it receives its own provider-list identity.
    if model == V7_MODEL:
        if bench_version >= 10:
            return V10_AGGREGATE_PROFILE_REVISION
        if bench_version == 9:
            return V9_AGGREGATE_PROFILE_REVISION
        return V7_AGGREGATE_PROFILE_REVISION
    # Exact upstream membership was already dynamic in aggregate mode: the
    # router could choose a different healthy provider on every request. Keep
    # operational provider health/order outside the calibrated identity while
    # pinning the parts that change the model contract or privacy boundary.
    profile = {
        "model": model,
        "provider": {
            "allow_fallbacks": True,
            "data_collection": "deny",
            "zdr": True,
        },
    }
    identity = json.dumps(profile, sort_keys=True, separators=(",", ":"))
    return f"openrouter-route-{hashlib.sha256(identity.encode()).hexdigest()[:16]}-v1"


def aggregate_provider(*, bench_version: int = 7) -> str:
    """Return the immutable logical gateway identity for a benchmark era."""
    return V10_AGGREGATE_PROVIDER if bench_version >= 10 else AGGREGATE_PROVIDER


def aggregate_profile_revisions(model: str) -> tuple[str, ...]:
    """Every aggregate profile discovery must keep visible for ``model``."""
    revisions = {
        aggregate_profile_revision(model, bench_version=7),
        aggregate_profile_revision(model, bench_version=9),
        aggregate_profile_revision(model, bench_version=10),
    }
    return tuple(sorted(revisions))


def benchmark_model(bench_version: int) -> str:
    return V7_MODEL if bench_version >= 7 else "qwen/qwen3-32b"


def _profile_revision(model: str, provider: str, quantization: str | None) -> str:
    identity = f"{model}\0{provider}\0{quantization or 'unknown'}"
    reasoning = benchmark_reasoning(model)
    if reasoning is not None:
        identity += "\0" + json.dumps(reasoning, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"openrouter-route-{digest}-v1"


@dataclass(frozen=True)
class RouteScore:
    route: InferenceProviderRoute
    utility: float


def rank_routes(
    routes: list[InferenceProviderRoute],
    *,
    speed_weight: float,
    cost_weight: float,
    exploration_weight: float,
    require_calibration: bool = True,
) -> list[RouteScore]:
    """Rank routes by reliability and efficiency, with legacy quality gating."""
    if not routes:
        return []
    measured_speeds = [
        route.ewma_tokens_per_second
        for route in routes
        if route.ewma_tokens_per_second is not None and route.ewma_tokens_per_second > 0
    ]
    max_speed = max(measured_speeds, default=1.0)
    costs = [
        (route.prompt_price_per_token or 0) + (route.completion_price_per_token or 0)
        for route in routes
        if (route.prompt_price_per_token or 0) + (route.completion_price_per_token or 0)
        > 0
    ]
    min_cost = min(costs, default=1.0)
    ranked: list[RouteScore] = []
    for route in routes:
        speed = (
            route.ewma_tokens_per_second / max_speed
            if route.ewma_tokens_per_second is not None
            and route.ewma_tokens_per_second > 0
            else 0.5
        )
        price = (route.prompt_price_per_token or 0) + (
            route.completion_price_per_token or 0
        )
        cost = min_cost / price if price > 0 else 0.5
        reliability = max(0.0, 1.0 - (route.ewma_error_rate or 0.0)) * max(
            0.0, 1.0 - (route.ewma_timeout_rate or 0.0)
        )
        # V7 multiplies by reviewed calibration quality. V8+ intentionally
        # omit that retired gate and rank only operational reliability,
        # throughput, and cost. Raw miner scores never influence either path.
        quality = (
            min(
                route.calibration_tool_accuracy or 0.0,
                route.calibration_composite or 0.0,
            )
            if require_calibration
            else 1.0
        )
        total_samples = sum(item.sample_count for item in routes)
        exploration = math.sqrt(
            2 * math.log(max(2, total_samples + 1)) / (route.sample_count + 1)
        )
        utility = (
            reliability
            * quality
            * (
                speed_weight * speed
                + cost_weight * cost
                + exploration_weight * exploration
            )
        )
        ranked.append(RouteScore(route=route, utility=utility))
    return sorted(
        ranked,
        key=lambda item: (
            -item.utility,
            item.route.provider.casefold(),
            item.route.profile_revision,
        ),
    )


async def select_route(
    session: Any,
    *,
    model: str,
    now: datetime,
    supported_profiles: tuple[str, ...] | None = None,
    calibration_manifest_sha256: str | None = None,
    routing_mode: str = "adaptive",
    bench_version: int = 7,
) -> InferenceProviderRoute | None:
    policy = await session.get(InferenceRoutingPolicy, model)
    if policy is None:
        return None
    require_calibration = benchmark_contract(
        bench_version
    ).requires_inference_route_calibration
    if routing_mode == "aggregate_throughput":
        profile = aggregate_profile_revision(model, bench_version=bench_version)
        provider = aggregate_provider(bench_version=bench_version)
        route = await session.get(
            InferenceProviderRoute,
            (model, provider, profile),
            with_for_update=True,
        )
        if (
            route is None
            or route.status not in {"discovered", "healthy", "degraded"}
            or (route.cooldown_until is not None and route.cooldown_until > now)
            or (
                require_calibration
                and (
                    route.calibration_status != "eligible"
                    or route.calibration_manifest_sha256 is None
                    or route.calibration_sample_count != AGGREGATE_CALIBRATION_SAMPLES
                    or (route.calibration_tool_accuracy or 0) < policy.min_tool_accuracy
                    or (route.calibration_composite or 0) < policy.min_composite
                    or (
                        supported_profiles is not None
                        and profile not in supported_profiles
                    )
                    or (
                        calibration_manifest_sha256 is not None
                        and route.calibration_manifest_sha256
                        != calibration_manifest_sha256
                    )
                )
            )
        ):
            return None
        route.selected_ticket_count += 1
        route.last_selected_at = now
        route.updated_at = now
        return route
    if routing_mode != "adaptive" or not policy.enabled:
        return None
    route_statement = select(InferenceProviderRoute).where(
        InferenceProviderRoute.model == model,
        InferenceProviderRoute.ewma_error_rate <= policy.max_error_rate,
        InferenceProviderRoute.ewma_timeout_rate <= policy.max_timeout_rate,
        InferenceProviderRoute.status.in_(("discovered", "healthy")),
    )
    if require_calibration:
        route_statement = route_statement.where(
            InferenceProviderRoute.calibration_status == "eligible",
            InferenceProviderRoute.calibration_tool_accuracy
            >= policy.min_tool_accuracy,
            InferenceProviderRoute.calibration_composite >= policy.min_composite,
            InferenceProviderRoute.calibration_sample_count
            >= policy.min_calibration_samples,
            InferenceProviderRoute.calibration_manifest_sha256.is_not(None),
        )
    routes = list((await session.scalars(route_statement.with_for_update())).all())
    routes = [
        route
        for route in routes
        if (route.cooldown_until is None or route.cooldown_until <= now)
        and (
            not require_calibration
            or (
                (
                    supported_profiles is None
                    or route.profile_revision in supported_profiles
                )
                and (
                    calibration_manifest_sha256 is None
                    or route.calibration_manifest_sha256 == calibration_manifest_sha256
                )
            )
        )
    ]
    explorers = sorted(
        (
            route
            for route in routes
            if route.exploration_ticket_count < policy.exploration_ticket_budget
        ),
        key=lambda route: (
            route.exploration_ticket_count,
            route.last_selected_at or route.discovered_at,
            route.provider.casefold(),
            route.profile_revision,
        ),
    )
    if explorers:
        selected = explorers[0]
        selected.exploration_ticket_count += 1
    else:
        ranked = rank_routes(
            routes,
            speed_weight=policy.speed_weight,
            cost_weight=policy.cost_weight,
            exploration_weight=policy.exploration_weight,
            require_calibration=require_calibration,
        )
        if not ranked:
            return None
        selected = ranked[0].route
    selected.selected_ticket_count += 1
    selected.last_selected_at = now
    selected.updated_at = now
    return selected


async def record_route_observation(
    session: Any,
    *,
    grant: InferenceGrant,
    success: bool,
    latency_ms: float,
    completion_tokens: int,
    cost_microusd: int,
    timed_out: bool,
    now: datetime,
) -> None:
    if not grant.route_provider:
        return
    if not grant.route_profile:
        return
    route = await session.get(
        InferenceProviderRoute,
        (grant.allowed_models[0], grant.route_provider, grant.route_profile),
        with_for_update=True,
    )
    if route is None:
        return
    policy = await session.get(InferenceRoutingPolicy, grant.allowed_models[0])
    if policy is None:
        return
    alpha = policy.ewma_alpha

    def ewma(previous: float | None, observed: float) -> float:
        return (
            observed if previous is None else alpha * observed + (1 - alpha) * previous
        )

    route.sample_count += 1
    route.ewma_latency_ms = ewma(route.ewma_latency_ms, max(0.0, latency_ms))
    if success and latency_ms > 0 and completion_tokens > 0:
        route.ewma_tokens_per_second = ewma(
            route.ewma_tokens_per_second,
            completion_tokens / (latency_ms / 1000),
        )
    route.ewma_error_rate = ewma(route.ewma_error_rate, 0.0 if success else 1.0)
    route.ewma_timeout_rate = ewma(route.ewma_timeout_rate, 1.0 if timed_out else 0.0)
    if cost_microusd >= 0:
        route.ewma_cost_microusd = ewma(route.ewma_cost_microusd, float(cost_microusd))
    route.status = "healthy" if success else "degraded"
    route.cooldown_until = (
        None if success else now + timedelta(seconds=policy.cooldown_seconds)
    )
    route.last_observed_at = now
    route.updated_at = now


async def record_route_quality(
    session: Any,
    *,
    grant: InferenceGrant,
    tool_accuracy: float,
    composite: float,
    now: datetime,
) -> None:
    """Fold an accepted score into the ticket's immutable provider route."""
    if not grant.route_provider:
        return
    if not grant.route_profile:
        return
    route = await session.get(
        InferenceProviderRoute,
        (grant.allowed_models[0], grant.route_provider, grant.route_profile),
        with_for_update=True,
    )
    if route is None:
        return
    policy = await session.get(InferenceRoutingPolicy, grant.allowed_models[0])
    if policy is None:
        return
    alpha = policy.ewma_alpha

    def ewma(previous: float | None, observed: float) -> float:
        return (
            observed if previous is None else alpha * observed + (1 - alpha) * previous
        )

    route.ewma_tool_accuracy = ewma(route.ewma_tool_accuracy, tool_accuracy)
    route.ewma_composite = ewma(route.ewma_composite, composite)
    route.updated_at = now


async def record_ticket_route_quality(
    session: Any,
    *,
    agent_id: Any,
    bench_version: int,
    validator_hotkey: str,
    ticket_deadline: datetime,
    tool_accuracy: float,
    composite: float,
    now: datetime,
) -> None:
    grant = await session.scalar(
        select(InferenceGrant).where(
            InferenceGrant.agent_id == agent_id,
            InferenceGrant.bench_version == bench_version,
            InferenceGrant.validator_hotkey == validator_hotkey,
            InferenceGrant.ticket_deadline == ticket_deadline,
        )
    )
    if grant is not None:
        await record_route_quality(
            session,
            grant=grant,
            tool_accuracy=tool_accuracy,
            composite=composite,
            now=now,
        )


class ProviderRouteRefresher:
    """Passively refresh the discoverable route inventory without API keys."""

    def __init__(self, *, config: Any, session_maker: Any, client: httpx.AsyncClient):
        self._config = config
        self._session_maker = session_maker
        self._client = client
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._config.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="inference-provider-discovery"
        )

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception as error:
                # Never log provider payloads or inference bodies.
                logger.warning(
                    "inference provider discovery failed (%s)",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.discovery_interval_seconds
                )
            except TimeoutError:
                continue

    async def refresh(self) -> None:
        now = datetime.now(UTC)
        for model in self._config.allowed_models:
            url = self._config.discovery_url_template.format(model=model)
            response = await self._client.get(url)
            response.raise_for_status()
            payload = response.json()
            endpoints = payload.get("data", {}).get("endpoints", [])
            if not isinstance(endpoints, list):
                continue
            deduped: dict[tuple[str, str], dict[str, Any]] = {}
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    continue
                provider = endpoint.get("provider_name")
                if not isinstance(provider, str) or not provider.strip():
                    continue
                quantization = endpoint.get("quantization")
                if not isinstance(quantization, str):
                    quantization = "unknown"
                profile = _profile_revision(model, provider, quantization)
                key = (provider, profile)
                current = deduped.get(key)
                if current is None or endpoint.get("status") == 0:
                    deduped[key] = endpoint
            async with self._session_maker() as session, session.begin():
                policy = await session.get(InferenceRoutingPolicy, model)
                if policy is None:
                    session.add(
                        InferenceRoutingPolicy(
                            model=model,
                            enabled=False,
                            speed_weight=self._config.route_speed_weight,
                            cost_weight=self._config.route_cost_weight,
                            exploration_weight=self._config.route_exploration_weight,
                            exploration_ticket_budget=(
                                self._config.route_exploration_ticket_budget
                            ),
                            min_tool_accuracy=self._config.route_min_tool_accuracy,
                            min_composite=self._config.route_min_composite,
                            min_calibration_samples=(
                                self._config.route_min_calibration_samples
                            ),
                            max_error_rate=self._config.route_max_error_rate,
                            max_timeout_rate=self._config.route_max_timeout_rate,
                            cooldown_seconds=self._config.route_cooldown_seconds,
                            ewma_alpha=self._config.route_ewma_alpha,
                            updated_at=now,
                        )
                    )
                existing = list(
                    (
                        await session.scalars(
                            select(InferenceProviderRoute).where(
                                InferenceProviderRoute.model == model
                            )
                        )
                    ).all()
                )
                seen: set[tuple[str, str]] = set()
                if model == benchmark_model(7):
                    aggregate_active = any(
                        endpoint.get("status") == 0 for endpoint in deduped.values()
                    )
                    for aggregate_profile in aggregate_profile_revisions(model):
                        aggregate_bench_version = 7
                        if aggregate_profile == V9_AGGREGATE_PROFILE_REVISION:
                            aggregate_bench_version = 9
                        elif aggregate_profile == V10_AGGREGATE_PROFILE_REVISION:
                            aggregate_bench_version = 10
                        aggregate_route_provider = aggregate_provider(
                            bench_version=aggregate_bench_version
                        )
                        aggregate_route = await session.get(
                            InferenceProviderRoute,
                            (model, aggregate_route_provider, aggregate_profile),
                        )
                        if aggregate_route is None:
                            aggregate_route = InferenceProviderRoute(
                                model=model,
                                provider=aggregate_route_provider,
                                profile_revision=aggregate_profile,
                                status=(
                                    "discovered" if aggregate_active else "offline"
                                ),
                                calibration_status="shadow",
                                discovered_at=now,
                                ewma_error_rate=0,
                                ewma_timeout_rate=0,
                                sample_count=0,
                                selected_ticket_count=0,
                                exploration_ticket_count=0,
                            )
                            session.add(aggregate_route)
                        elif aggregate_active and aggregate_route.status == "offline":
                            aggregate_route.status = "discovered"
                        elif not aggregate_active:
                            aggregate_route.status = "offline"
                        aggregate_route.quantization = None
                        aggregate_route.updated_at = now
                        seen.add((aggregate_route_provider, aggregate_profile))
                for (provider, profile), endpoint in deduped.items():
                    quantization = endpoint.get("quantization")
                    if not isinstance(quantization, str):
                        quantization = None
                    route = await session.get(
                        InferenceProviderRoute, (model, provider, profile)
                    )
                    seen.add((provider, profile))
                    pricing = endpoint.get("pricing")
                    if not isinstance(pricing, dict):
                        pricing = {}

                    def price(
                        name: str, values: dict[str, Any] = pricing
                    ) -> float | None:
                        raw = values.get(name)
                        if not isinstance(raw, (str, int, float)) or isinstance(
                            raw, bool
                        ):
                            return None
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            return None
                        return value if value >= 0 else None

                    active = endpoint.get("status") == 0
                    if route is None:
                        route = InferenceProviderRoute(
                            model=model,
                            provider=provider,
                            profile_revision=profile,
                            status="discovered" if active else "offline",
                            calibration_status="shadow",
                            discovered_at=now,
                            ewma_error_rate=0,
                            ewma_timeout_rate=0,
                            sample_count=0,
                            selected_ticket_count=0,
                            exploration_ticket_count=0,
                        )
                        session.add(route)
                    if not active:
                        route.status = "offline"
                    elif route.status == "offline":
                        route.status = "discovered"
                    route.context_length = (
                        endpoint.get("context_length")
                        if isinstance(endpoint.get("context_length"), int)
                        else None
                    )
                    route.quantization = quantization
                    route.prompt_price_per_token = price("prompt")
                    route.completion_price_per_token = price("completion")
                    route.updated_at = now
                for route in existing:
                    if (route.provider, route.profile_revision) not in seen:
                        route.status = "offline"
                        route.updated_at = now


__all__ = [
    "AGGREGATE_CALIBRATION_SAMPLES",
    "AGGREGATE_PROVIDER",
    "V10_AGGREGATE_PROVIDER",
    "aggregate_provider",
    "ProviderRouteRefresher",
    "aggregate_profile_revision",
    "benchmark_model",
    "benchmark_reasoning",
    "rank_routes",
    "record_route_observation",
    "record_route_quality",
    "record_ticket_route_quality",
    "select_route",
]
