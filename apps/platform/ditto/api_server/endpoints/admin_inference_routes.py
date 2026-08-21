"""Audited admission controls for benchmark inference routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_inference_routes import (
    AdminInferenceRoutes,
    AggregateRouteView,
    GatewayProviderView,
    InferenceRouteView,
    InferenceRoutingAuditView,
    InferenceRoutingPolicyView,
    ProviderTelemetryView,
    RelayRecoveryTelemetryView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    AGGREGATE_PROVIDER,
    aggregate_profile_revision,
    aggregate_profile_revisions,
    aggregate_provider,
    benchmark_model,
)
from ditto.db.models import (
    InferenceGatewayAttempt,
    InferenceProviderRoute,
    InferenceRoutingAudit,
    InferenceRoutingPolicy,
    ValidatorTicket,
)

router = APIRouter(prefix="/admin/inference-routes", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _require_actor(value: str | None) -> str:
    actor = value.strip() if value is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


class RouteCalibrationRequest(BaseModel):
    """Exact reviewed manifest decision for one immutable route profile."""

    model_config = ConfigDict(extra="ignore")

    model: str
    provider: str
    expected_revision: Annotated[int, Field(ge=0)]
    action: Literal["eligible", "shadow", "disabled"]
    manifest_sha256: _Digest
    tool_accuracy: Annotated[float, Field(ge=0, le=1)]
    composite: Annotated[float, Field(ge=0, le=1)]
    sample_count: Annotated[int, Field(ge=1)]
    confirmation: str


class RoutingPolicyRequest(BaseModel):
    """Complete auditable replacement for one model's routing policy."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool
    gateway_provider_order: Annotated[list[str], Field(min_length=1, max_length=2)]
    expected_revision: Annotated[int, Field(ge=0)]
    speed_weight: Annotated[float, Field(ge=0, le=1)]
    cost_weight: Annotated[float, Field(ge=0, le=1)]
    exploration_weight: Annotated[float, Field(ge=0, le=1)]
    exploration_ticket_budget: Annotated[int, Field(ge=0, le=100)]
    min_tool_accuracy: Annotated[float, Field(ge=0, le=1)]
    min_composite: Annotated[float, Field(ge=0, le=1)]
    min_calibration_samples: Annotated[int, Field(ge=1, le=10_000)]
    max_error_rate: Annotated[float, Field(ge=0, le=1)]
    max_timeout_rate: Annotated[float, Field(ge=0, le=1)]
    cooldown_seconds: Annotated[int, Field(ge=1, le=3600)]
    ewma_alpha: Annotated[float, Field(gt=0, le=1)]
    confirmation: str


@router.get("", response_model=AdminInferenceRoutes)
async def list_inference_routes(
    _: AdminDep, session: SessionDep, request: Request, response: Response
) -> AdminInferenceRoutes:
    """The operator console's whole inference-routing inventory.

    Typed rather than ``dict[str, object]`` because the aggregates below come
    back from asyncpg as ``Decimal`` (``sum``/``avg`` are ``numeric`` in
    Postgres), and an untyped ``object`` slot makes Pydantic v2 serialize a
    ``Decimal`` as a JSON *string*. The declared ``int``/``float`` fields are
    what keep this endpoint serving numbers.
    """
    response.headers["Cache-Control"] = "no-store"
    rows = list(
        (
            await session.scalars(
                select(InferenceProviderRoute).order_by(
                    InferenceProviderRoute.model,
                    InferenceProviderRoute.provider,
                    InferenceProviderRoute.profile_revision,
                )
            )
        ).all()
    )
    policies = list(
        (
            await session.scalars(
                select(InferenceRoutingPolicy).order_by(InferenceRoutingPolicy.model)
            )
        ).all()
    )
    audits = list(
        (
            await session.scalars(
                select(InferenceRoutingAudit)
                .order_by(InferenceRoutingAudit.recorded_at.desc())
                .limit(100)
            )
        ).all()
    )
    provider_identity = InferenceGatewayAttempt.gateway_provider
    provider_telemetry = list(
        (
            await session.execute(
                select(
                    provider_identity.label("upstream_provider"),
                    func.count().label("request_count"),
                    func.sum(
                        case(
                            (InferenceGatewayAttempt.status == "completed", 1), else_=0
                        )
                    ).label("completed_count"),
                    func.sum(
                        case((InferenceGatewayAttempt.status == "failed", 1), else_=0)
                    ).label("failed_count"),
                    func.sum(
                        case((InferenceGatewayAttempt.status == "started", 1), else_=0)
                    ).label("inflight_count"),
                    func.sum(
                        case((InferenceGatewayAttempt.timed_out.is_(True), 1), else_=0)
                    ).label("timeout_count"),
                    func.sum(InferenceGatewayAttempt.upstream_attempts).label(
                        "upstream_attempt_count"
                    ),
                    func.sum(InferenceGatewayAttempt.openrouter_attempts).label(
                        "openrouter_attempt_count"
                    ),
                    func.sum(
                        case(
                            (
                                (InferenceGatewayAttempt.status == "completed")
                                & (InferenceGatewayAttempt.phase > 0),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("recovered_after_fallback_count"),
                    func.sum(
                        case(
                            (
                                (InferenceGatewayAttempt.status == "failed")
                                & InferenceGatewayAttempt.terminal_error_code.is_not(
                                    None
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("terminal_failure_count"),
                    func.sum(InferenceGatewayAttempt.prompt_tokens).label(
                        "prompt_tokens"
                    ),
                    func.sum(InferenceGatewayAttempt.completion_tokens).label(
                        "completion_tokens"
                    ),
                    func.sum(InferenceGatewayAttempt.cost_microusd).label(
                        "cost_microusd"
                    ),
                    func.sum(
                        case(
                            (
                                (InferenceGatewayAttempt.status == "completed")
                                & InferenceGatewayAttempt.cost_available.is_(False),
                                1,
                            ),
                            else_=0,
                        )
                    ).label("missing_cost_count"),
                    func.avg(InferenceGatewayAttempt.latency_ms).label(
                        "average_latency_ms"
                    ),
                    (
                        func.sum(
                            case(
                                (
                                    InferenceGatewayAttempt.status == "completed",
                                    InferenceGatewayAttempt.completion_tokens,
                                ),
                                else_=0,
                            )
                        )
                        * 1000.0
                        / func.nullif(
                            func.sum(
                                case(
                                    (
                                        InferenceGatewayAttempt.status == "completed",
                                        InferenceGatewayAttempt.latency_ms,
                                    ),
                                    else_=0,
                                )
                            ),
                            0,
                        )
                    ).label("observed_output_tps"),
                )
                .group_by(provider_identity)
                .order_by(provider_identity)
            )
        ).all()
    )
    relay_recovery = (
        await session.execute(
            select(
                func.sum(
                    case(
                        (
                            (ValidatorTicket.failure_reason == "infrastructure")
                            & ValidatorTicket.failure_detail.like(
                                "model_relay_unavailable%"
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("benchmark_relay_abort_ticket_count"),
                func.sum(
                    case(
                        (
                            ValidatorTicket.failure_detail
                            == "model_relay_unavailable:provider_recovery_exhausted",
                            1,
                        ),
                        else_=0,
                    )
                ).label("broker_recovery_exhausted_ticket_count"),
            )
        )
    ).one()
    inference_config = request.app.state.config.inference_proxy
    routing_mode = inference_config.routing_mode
    aggregate_model = benchmark_model(7)
    return AdminInferenceRoutes(
        routing_mode=routing_mode,
        aggregate_route=(
            AggregateRouteView(
                model=aggregate_model,
                provider=aggregate_provider(bench_version=10),
                profile_revision=aggregate_profile_revision(
                    aggregate_model, bench_version=10
                ),
                provider_sort="throughput",
                provider_order=[],
                reliability_provider_order=["DeepInfra", "Groq"],
                ignored_providers=["CoreWeave"],
                allow_fallbacks=True,
            )
            if routing_mode == "aggregate_throughput"
            else None
        ),
        gateway_providers=[
            GatewayProviderView(
                provider=provider,
                configured=provider
                in {item.name for item in inference_config.chat_providers},
            )
            for provider in ("instant", "openrouter")
        ],
        policies=[
            InferenceRoutingPolicyView(
                model=policy.model,
                revision=policy.revision,
                enabled=policy.enabled,
                gateway_provider_order=policy.gateway_provider_order,
                speed_weight=policy.speed_weight,
                cost_weight=policy.cost_weight,
                exploration_weight=policy.exploration_weight,
                exploration_ticket_budget=policy.exploration_ticket_budget,
                min_tool_accuracy=policy.min_tool_accuracy,
                min_composite=policy.min_composite,
                min_calibration_samples=policy.min_calibration_samples,
                max_error_rate=policy.max_error_rate,
                max_timeout_rate=policy.max_timeout_rate,
                cooldown_seconds=policy.cooldown_seconds,
                ewma_alpha=policy.ewma_alpha,
                updated_at=policy.updated_at,
            )
            for policy in policies
        ],
        routes=[
            InferenceRouteView(
                model=row.model,
                provider=row.provider,
                profile_revision=row.profile_revision,
                quantization=row.quantization,
                status=row.status,
                calibration_status=row.calibration_status,
                calibration_revision=row.calibration_revision,
                calibration_manifest_sha256=row.calibration_manifest_sha256,
                calibration_sample_count=row.calibration_sample_count,
                calibration_tool_accuracy=row.calibration_tool_accuracy,
                calibration_composite=row.calibration_composite,
                sample_count=row.sample_count,
                selected_ticket_count=row.selected_ticket_count,
                exploration_ticket_count=row.exploration_ticket_count,
                last_selected_at=row.last_selected_at,
                ewma_tokens_per_second=row.ewma_tokens_per_second,
                ewma_latency_ms=row.ewma_latency_ms,
                ewma_error_rate=row.ewma_error_rate,
                ewma_timeout_rate=row.ewma_timeout_rate,
                prompt_price_per_token=row.prompt_price_per_token,
                completion_price_per_token=row.completion_price_per_token,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
        audits=[
            InferenceRoutingAuditView(
                audit_id=str(audit.audit_id),
                actor=audit.actor,
                action=audit.action,
                model=audit.model,
                profile_revision=audit.profile_revision,
                payload=audit.payload,
                recorded_at=audit.recorded_at,
            )
            for audit in audits
        ],
        provider_telemetry=[
            ProviderTelemetryView(
                provider=row.upstream_provider,
                request_count=row.request_count,
                completed_count=row.completed_count,
                failed_count=row.failed_count,
                inflight_count=row.inflight_count,
                timeout_count=row.timeout_count,
                upstream_attempt_count=row.upstream_attempt_count,
                openrouter_attempt_count=row.openrouter_attempt_count,
                recovered_after_fallback_count=row.recovered_after_fallback_count,
                terminal_failure_count=row.terminal_failure_count,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cost_microusd=row.cost_microusd,
                cost_available=row.missing_cost_count == 0,
                average_latency_ms=row.average_latency_ms,
                observed_output_tps=row.observed_output_tps,
            )
            for row in provider_telemetry
        ],
        relay_recovery_telemetry=RelayRecoveryTelemetryView(
            benchmark_relay_abort_ticket_count=(
                relay_recovery.benchmark_relay_abort_ticket_count or 0
            ),
            broker_recovery_exhausted_ticket_count=(
                relay_recovery.broker_recovery_exhausted_ticket_count or 0
            ),
        ),
    )


@router.put("/policy/{model:path}")
async def update_routing_policy(
    _: AdminDep,
    model: str,
    payload: RoutingPolicyRequest,
    session: SessionDep,
    request: Request,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    actor = _require_actor(x_admin_actor)
    expected = f"UPDATE INFERENCE POLICY {model}"
    if payload.confirmation != expected:
        raise HTTPException(status_code=409, detail=f'type "{expected}" exactly')
    if payload.speed_weight + payload.cost_weight + payload.exploration_weight <= 0:
        raise HTTPException(
            status_code=409, detail="routing weights cannot all be zero"
        )
    configured = {
        provider.name
        for provider in request.app.state.config.inference_proxy.chat_providers
    }
    if len(payload.gateway_provider_order) != len(
        set(payload.gateway_provider_order)
    ) or not set(payload.gateway_provider_order).issubset(configured):
        raise HTTPException(
            status_code=409,
            detail="gateway provider order must contain unique configured providers",
        )
    policy = await session.get(InferenceRoutingPolicy, model, with_for_update=True)
    if policy is None:
        raise HTTPException(status_code=404, detail="unknown inference model policy")
    if policy.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="inference policy changed; refresh")
    for field in (
        "enabled",
        "gateway_provider_order",
        "speed_weight",
        "cost_weight",
        "exploration_weight",
        "exploration_ticket_budget",
        "min_tool_accuracy",
        "min_composite",
        "min_calibration_samples",
        "max_error_rate",
        "max_timeout_rate",
        "cooldown_seconds",
        "ewma_alpha",
    ):
        setattr(policy, field, getattr(payload, field))
    policy.updated_at = datetime.now(UTC)
    policy.revision += 1
    session.add(
        InferenceRoutingAudit(
            audit_id=uuid4(),
            actor=actor,
            action="policy_updated",
            model=model,
            profile_revision=None,
            payload=payload.model_dump(exclude={"confirmation"}),
            recorded_at=policy.updated_at,
        )
    )
    await session.commit()
    return {
        "model": policy.model,
        "enabled": policy.enabled,
        "revision": policy.revision,
    }


@router.post("/{profile_revision}/calibration")
async def calibrate_inference_route(
    _: AdminDep,
    profile_revision: str,
    payload: RouteCalibrationRequest,
    session: SessionDep,
    request: Request,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    inference_config = request.app.state.config.inference_proxy
    routing_mode = inference_config.routing_mode
    if routing_mode == "aggregate_throughput" and (
        payload.provider
        not in {AGGREGATE_PROVIDER, aggregate_provider(bench_version=10)}
        or profile_revision not in aggregate_profile_revisions(payload.model)
    ):
        raise HTTPException(
            status_code=409,
            detail="provider-specific route admission is disabled by rollout mode",
        )
    actor = _require_actor(x_admin_actor)
    expected = f"{payload.action.upper()} INFERENCE ROUTE {profile_revision}"
    if payload.confirmation != expected:
        raise HTTPException(status_code=409, detail=f'type "{expected}" exactly')
    route = await session.scalar(
        select(InferenceProviderRoute)
        .where(
            InferenceProviderRoute.profile_revision == profile_revision,
            InferenceProviderRoute.model == payload.model,
            InferenceProviderRoute.provider == payload.provider,
        )
        .with_for_update()
    )
    if route is None:
        raise HTTPException(status_code=404, detail="unknown inference route profile")
    if route.calibration_revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="inference route changed; refresh")
    policy = await session.get(InferenceRoutingPolicy, payload.model)
    if policy is None:
        raise HTTPException(
            status_code=409, detail="inference routing policy is missing"
        )
    if payload.action == "eligible" and (
        route.status not in {"discovered", "healthy"}
        or (
            routing_mode == "aggregate_throughput"
            and payload.sample_count != AGGREGATE_CALIBRATION_SAMPLES
        )
        or payload.sample_count < policy.min_calibration_samples
        or payload.tool_accuracy < policy.min_tool_accuracy
        or payload.composite < policy.min_composite
    ):
        raise HTTPException(
            status_code=409,
            detail="route does not meet reviewed calibration and health floors",
        )
    if payload.action == "eligible" and (
        inference_config.reviewed_calibration_manifest_sha256 is None
        or payload.manifest_sha256
        != inference_config.reviewed_calibration_manifest_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="calibration manifest is not the deployed reviewed artifact",
        )
    now = datetime.now(UTC)
    route.calibration_status = payload.action
    route.calibration_manifest_sha256 = payload.manifest_sha256
    route.calibration_tool_accuracy = payload.tool_accuracy
    route.calibration_composite = payload.composite
    route.calibration_sample_count = payload.sample_count
    route.calibrated_at = now
    route.updated_at = now
    route.calibration_revision += 1
    session.add(
        InferenceRoutingAudit(
            audit_id=uuid4(),
            actor=actor,
            action=f"route_{payload.action}",
            model=payload.model,
            profile_revision=route.profile_revision,
            payload=payload.model_dump(exclude={"confirmation"}),
            recorded_at=now,
        )
    )
    await session.commit()
    return {
        "profile_revision": route.profile_revision,
        "calibration_status": route.calibration_status,
        "calibration_manifest_sha256": route.calibration_manifest_sha256,
        "calibration_revision": route.calibration_revision,
    }


__all__ = ["router"]
