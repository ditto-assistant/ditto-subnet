"""Audited operator control for bounded benchmark cohort qualification."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.benchmark_contract import (
    benchmark_contract,
    benchmark_contracts,
)
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.api_server.benchmark_rollout import (
    expand_rolling_qualification,
    inference_activation_requirements,
    qualification_candidate,
    refresh_rolling_qualification,
)
from ditto.api_server.datapipeline import DataPipelineError, DatasetGenerator
from ditto.api_server.dependencies import get_dataset_generator, get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    AGGREGATE_PROVIDER,
    aggregate_profile_revision,
    benchmark_model,
)
from ditto.api_server.queue_policy_settings import (
    resolve_queue_policy_settings,
)
from ditto.db.models import (
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    ValidatorHeartbeat,
)
from ditto.db.queries.benchmark_rollout import (
    MIN_SCOREABLE_BENCH_VERSION,
    PRIORITY_COHORT_SIZE,
    DatasetPin,
    RolloutConflictError,
    active_bench_version,
    authority_selection_state,
    capable_validator_counts,
    create_rollout_snapshot,
    heartbeat_supports_version,
    historical_rescore_cohort,
    rollout_for_desired_version,
    rollout_state,
    select_active_bench_version,
    supersede_open_rollout,
)

router = APIRouter(prefix="/admin/benchmark-rollout", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
GeneratorDep = Annotated[DatasetGenerator, Depends(get_dataset_generator)]
AdminDep = Annotated[None, Depends(require_admin)]
MINIMUM_ROLLOUT_START_VALIDATORS = 1
# Wall-clock budget for one read of the rollout status. Deliberately shorter
# than the operator console's own request timeout so a slow read comes back as
# a named 503 (or a flagged partial) from this service, rather than as a client
# abort that names no dependency and reads like an auth failure.
ROLLOUT_STATUS_BUDGET_SECONDS = 12.0

_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3)]
_Actor = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class AdminRolloutSupersedeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: _Reason
    actor: _Actor = "admin_api"
    confirmation: str


class AdminRolloutStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: _Reason
    actor: _Actor = "admin_api"
    confirmation: str
    expected_active_version: int


class AdminRolloutExpandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: _Reason
    actor: _Actor = "admin_api"
    confirmation: str
    expected_active_version: int
    expected_current_target: int
    new_target: int


class AdminActiveContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: _Reason
    actor: _Actor = "admin_api"
    confirmation: str
    expected_active_version: int


def _remaining(deadline: float) -> float:
    """Seconds left before ``deadline``, never negative enough to skip the await."""
    return max(deadline - monotonic(), 0.0)


def _parse_desired_version(desired_version: str) -> int:
    """Accept both the legacy ``/v3`` path and a bare ``/4``.

    Keeping the ``v`` prefix parseable is what preserves every already-deployed
    caller pinned to ``/admin/benchmark-rollout/v3``.
    """
    text = (
        desired_version[1:] if desired_version[:1].lower() == "v" else desired_version
    )
    if not text.isdigit():
        raise HTTPException(status_code=404, detail="unknown benchmark rollout version")
    version = int(text)
    try:
        benchmark_contract(version)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"benchmark version {version} has no shipped contract",
        ) from exc
    return version


def _generator_unavailable(target: int, exc: DataPipelineError) -> HTTPException:
    """502 for a generate-service that cannot render the target benchmark.

    The generator is deployed separately and pinned to a datagen release, so it
    lags this API whenever a new benchmark ships. Left unhandled this reached the
    operator as a bare 500 naming nothing -- not the dependency, not the version,
    not the fix.
    """
    return HTTPException(
        status_code=502,
        detail=(
            f"dataset generator could not render benchmark v{target} ({exc}). "
            f"Deploy the generate-service at a datagen release that ships "
            f"v{target} before starting this rollout."
        ),
    )


async def _require_rollout_start_capacity(
    session: AsyncSession,
    *,
    now: datetime,
    desired_version: int,
    routing_mode: str = "adaptive",
    reviewed_manifest_sha256: str | None = None,
) -> dict[str, object]:
    """Fail closed until one independently verified target scorer is online."""
    state = await rollout_state(session, now=now, capability_version=desired_version)
    capable = int(state["canary_capable_validator_count"])
    if capable < MINIMUM_ROLLOUT_START_VALIDATORS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"benchmark v{desired_version} rollout requires at least "
                f"{MINIMUM_ROLLOUT_START_VALIDATORS} fresh, identity-matched "
                "v8 scorer validators"
            ),
        )
    if desired_version >= 7:
        model = benchmark_model(desired_version)
        route_statement = (
            select(InferenceProviderRoute)
            .join(
                InferenceRoutingPolicy,
                InferenceRoutingPolicy.model == InferenceProviderRoute.model,
            )
            .where(
                InferenceProviderRoute.model == model,
                (
                    InferenceRoutingPolicy.enabled.is_(True)
                    if routing_mode == "adaptive"
                    else InferenceProviderRoute.provider == AGGREGATE_PROVIDER
                ),
                (
                    InferenceProviderRoute.profile_revision
                    == aggregate_profile_revision(model, bench_version=desired_version)
                    if routing_mode == "aggregate_throughput"
                    else InferenceProviderRoute.provider.is_not(None)
                ),
                InferenceProviderRoute.status.in_(("discovered", "healthy")),
                InferenceProviderRoute.calibration_status == "eligible",
                InferenceProviderRoute.calibration_tool_accuracy
                >= InferenceRoutingPolicy.min_tool_accuracy,
                InferenceProviderRoute.calibration_composite
                >= InferenceRoutingPolicy.min_composite,
                (
                    InferenceProviderRoute.calibration_sample_count
                    == AGGREGATE_CALIBRATION_SAMPLES
                    if routing_mode == "aggregate_throughput"
                    else InferenceProviderRoute.calibration_sample_count
                    >= InferenceRoutingPolicy.min_calibration_samples
                ),
                InferenceProviderRoute.calibration_manifest_sha256.is_not(None),
            )
        )
        if reviewed_manifest_sha256 is not None:
            route_statement = route_statement.where(
                InferenceProviderRoute.calibration_manifest_sha256
                == reviewed_manifest_sha256
            )
        calibrated_routes = list(await session.scalars(route_statement))
        if not calibrated_routes:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"benchmark v{desired_version} rollout requires at least one "
                    "healthy reviewed inference calibration"
                ),
            )
        heartbeats = list(await session.scalars(select(ValidatorHeartbeat)))
        matching_identity = False
        for heartbeat in heartbeats:
            if not heartbeat_supports_version(
                heartbeat, now=now, version=desired_version
            ):
                continue
            try:
                capabilities = ValidatorCapabilities.model_validate_json(
                    json.dumps(heartbeat.capabilities)
                )
            except ValidationError:
                continue
            scorer = capabilities.scorer_benchmarks
            calibration = scorer.v7_calibration if scorer is not None else None
            if calibration is None:
                continue
            supported = {
                (route.model, route.provider, route.profile_revision)
                for route in calibration.supported_routes
            }
            if any(
                row.calibration_manifest_sha256 == calibration.manifest_sha256
                and (row.model, row.provider, row.profile_revision) in supported
                for row in calibrated_routes
            ):
                matching_identity = True
                break
        if not matching_identity:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"benchmark v{desired_version} rollout requires a capable "
                    "validator whose exact route and manifest match an eligible "
                    "inference calibration"
                ),
            )
    return state


def _require_confirmation(actual: str, *, action: str, version: int) -> None:
    expected = f"{action} BENCHMARK V{version}"
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail=f'type "{expected}" exactly to confirm this operation',
        )


@router.get("")
async def get_rollout_control(
    _: AdminDep,
    session: SessionDep,
) -> dict[str, object]:
    """Advertise shipped targets and durable state without changing either.

    Strictly read-only and deadline-bounded. Nothing here renders a dataset,
    calls the generate-service, or writes a row: the operator console loads this
    on every page view, and a rollout only ever moves on a deliberate POST.

    The per-contract validator count comes from one heartbeat read rather than a
    full :func:`rollout_state` per shipped contract. The qualification lookups
    that remain are the genuinely heavy part, so they run under the request's
    remaining budget and degrade to an empty, explicitly-flagged list instead of
    holding the whole page hostage. Empty is the fail-closed answer: the console
    offers no activation it cannot prove is qualified.
    """
    deadline = monotonic() + ROLLOUT_STATUS_BUDGET_SECONDS
    contracts = benchmark_contracts()
    try:
        async with asyncio.timeout(_remaining(deadline)):
            state = await rollout_state(session)
            capability_counts = await capable_validator_counts(
                session, versions=[contract.version for contract in contracts]
            )
    except TimeoutError as exc:
        await session.invalidate()
        raise HTTPException(
            status_code=503,
            detail=(
                "benchmark rollout status exceeded its "
                f"{ROLLOUT_STATUS_BUDGET_SECONDS:.0f}s read budget. The platform "
                "database, not the operator token, is the dependency to check."
            ),
        ) from exc
    active = int(state["active_version"])
    # Above the active version AND at or above the floor. The second half is
    # not redundant when the ledger has no activation on record: `active` then
    # falls back to the floor, but a console that listed every shipped contract
    # above it would still offer retired targets that the start path now
    # refuses. Do not offer what cannot be started.
    targets = [
        contract.version
        for contract in contracts
        if contract.version > active and contract.version >= MIN_SCOREABLE_BENCH_VERSION
    ]
    degraded: list[str] = []
    candidates: list[dict[str, object]] = []
    try:
        async with asyncio.timeout(_remaining(deadline)):
            for version in targets:
                candidates.append(
                    await authority_selection_state(session, bench_version=version)
                )
    except TimeoutError:
        await session.invalidate()
        candidates = []
        degraded = ["active_contract_candidates"]
    return {
        **state,
        "contracts": [
            {
                "version": contract.version,
                "minimum_screening_policy_version": (
                    contract.minimum_screening_policy_version
                ),
                "requires_screened_image": contract.requires_screened_image,
                "capable_validator_count": capability_counts[contract.version],
            }
            for contract in contracts
        ],
        "available_target_versions": targets,
        "active_contract_candidates": candidates,
        "degraded_sections": degraded,
    }


@router.get("/{desired_version}")
async def get_rollout(
    _: AdminDep,
    session: SessionDep,
    desired_version: str,
) -> dict[str, object]:
    """Return rollout telemetry without starting or refreshing qualification."""
    target = _parse_desired_version(desired_version)
    return await rollout_state(session, capability_version=target)


@router.post("/{desired_version}/supersede")
async def supersede_rollout(
    _: AdminDep,
    session: SessionDep,
    desired_version: str,
    payload: AdminRolloutSupersedeRequest,
) -> dict[str, object]:
    """Terminally abandon the open rollout so a newer one can be opened.

    Refuses an activated rollout: activation already moved chain weights and
    released the superseded corpus, so it is history, not state.
    """
    target = _parse_desired_version(desired_version)
    _require_confirmation(payload.confirmation, action="SUPERSEDE", version=target)
    now = datetime.now(UTC)
    try:
        rollout = await supersede_open_rollout(
            session, actor=payload.actor, reason=payload.reason, now=now
        )
    except RolloutConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if rollout is None:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="no open benchmark rollout to supersede"
        )
    if rollout.desired_version != target:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"the open rollout targets v{rollout.desired_version}, not v{target}"
            ),
        )
    await session.commit()
    return await rollout_state(session, capability_version=target)


@router.post("/{desired_version}/expand")
async def expand_rollout(
    _: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
    desired_version: str,
    payload: AdminRolloutExpandRequest,
) -> dict[str, object]:
    """Explicitly append a larger ordered suffix to one open rollout."""
    target = _parse_desired_version(desired_version)
    expected_confirmation = f"EXPAND BENCHMARK V{target} TO {payload.new_target}"
    if payload.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f'type "{expected_confirmation}" exactly to confirm this operation',
        )
    try:
        result = await expand_rolling_qualification(
            session,
            generator=generator,
            now=datetime.now(UTC),
            desired_version=target,
            expected_active_version=payload.expected_active_version,
            expected_current_target=payload.expected_current_target,
            new_target=payload.new_target,
            actor=payload.actor,
            reason=payload.reason,
        )
    except DataPipelineError as exc:
        raise _generator_unavailable(target, exc) from exc
    except RolloutConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    state = await rollout_state(session, capability_version=target)
    state["expansion"] = {
        "previous_target": result.previous_target,
        "new_target": result.new_target,
        "appended_members": result.appended_members,
    }
    return state


@router.post("/{desired_version}/select-active")
async def select_active_contract(
    _: AdminDep,
    session: SessionDep,
    desired_version: str,
    payload: AdminActiveContractRequest,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Select a fully qualified superseded contract as weight authority."""
    target = _parse_desired_version(desired_version)
    _require_confirmation(payload.confirmation, action="ACTIVATE", version=target)
    current = await active_bench_version(session)
    if payload.expected_active_version != current:
        raise HTTPException(
            status_code=409,
            detail=(
                "active benchmark changed: expected "
                f"v{payload.expected_active_version}, found v{current}"
            ),
        )
    try:
        await select_active_bench_version(
            session,
            bench_version=target,
            actor=payload.actor,
            reason=payload.reason,
            now=datetime.now(UTC),
            inference_requirements=inference_activation_requirements(
                (
                    request.app.state.config.inference_proxy
                    if request is not None
                    else None
                ),
                bench_version=target,
            ),
        )
    except RolloutConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return await get_rollout_control(None, session)


@router.post("/{desired_version}")
async def start_rollout(
    _: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
    desired_version: str,
    payload: AdminRolloutStartRequest,
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, object]:
    """Seed the bounded historical rescore cohort and target datasets."""
    target = _parse_desired_version(desired_version)
    _require_confirmation(payload.confirmation, action="START", version=target)
    # The retired-era floor, named before anything else touches the database.
    #
    # ``benchmark_rollout_desired_floor`` would refuse the INSERT anyway, but a
    # CheckViolation escaping here is an unhandled IntegrityError -- a 500 on an
    # operator action, with the constraint name as the only explanation. The
    # forward-only guard below already answers this shape of mistake with a 409
    # naming both versions, and so should this one.
    if target < MIN_SCOREABLE_BENCH_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                f"benchmark v{target} is retired; a rollout can only target "
                f"v{MIN_SCOREABLE_BENCH_VERSION} or later"
            ),
        )
    from_version = await active_bench_version(session)
    if payload.expected_active_version != from_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "active benchmark changed: expected "
                f"v{payload.expected_active_version}, found v{from_version}"
            ),
        )
    # Idempotence first: a rollout already targeting this version is returned
    # as-is, whatever it started from. Only then does the forward-only guard
    # apply, so re-POSTing an already-activated version is not a 409.
    existing = await rollout_for_desired_version(session, desired_version=target)
    if (
        existing is not None
        and existing.status == "superseded"
        and existing.from_version != from_version
    ):
        # A recovered authority creates a new transition lineage. The terminal
        # superseded row remains immutable history; it must not make the fresh
        # active->target transition look idempotently complete.
        existing = None
    if existing is None and target <= from_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot start benchmark {target} rollout while "
                f"active benchmark is {from_version}"
            ),
        )
    if existing is not None:
        await session.rollback()  # close the read-only autobegin before the service
        # This refresh renders datasets too, so a lagging generate-service fails
        # here exactly as it does on the fresh-start path below. Re-POSTing a
        # version whose rollout already exists is the natural operator retry, so
        # it must not be the one route that still answers with an opaque 500.
        #
        # Unlike validator/screener ingest -- which deliberately swallow this so a
        # renderer outage cannot fail an already-committed score or verdict -- an
        # admin POST exists to report whether the action succeeded, so it surfaces.
        try:
            await refresh_rolling_qualification(
                session,
                generator=generator,
                now=datetime.now(UTC),
                inference_config=(
                    request.app.state.config.inference_proxy
                    if request is not None
                    else None
                ),
            )
        except DataPipelineError as exc:
            raise _generator_unavailable(target, exc) from exc
        return await rollout_state(session, capability_version=target)
    now = datetime.now(UTC)
    inference_config = (
        request.app.state.config.inference_proxy if request is not None else None
    )
    if (
        target >= 7
        and inference_config is not None
        and (
            not inference_config.enabled
            or inference_config.openrouter_api_key is None
            or inference_config.reviewed_calibration_manifest_sha256 is None
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"benchmark v{target} rollout requires the enabled platform "
                "inference proxy and a deployed reviewed calibration manifest"
            ),
        )
    await _require_rollout_start_capacity(
        session,
        now=now,
        desired_version=target,
        routing_mode=(
            inference_config.routing_mode
            if inference_config is not None
            else "adaptive"
        ),
        reviewed_manifest_sha256=(
            inference_config.reviewed_calibration_manifest_sha256
            if target >= 7 and inference_config is not None
            else None
        ),
    )
    # Read the operator policy exactly once, here, and freeze it onto the
    # rollout below. Everything downstream (backfill, blockers, telemetry) reads
    # the frozen value, so a later revision cannot resize this rollout.
    queue_policy = await resolve_queue_policy_settings(session)
    rescore_cohort_target = queue_policy.rescore_cohort_size
    priority_cohort_target = queue_policy.priority_cohort_size
    members = await historical_rescore_cohort(
        session, source_version=from_version, limit=rescore_cohort_target
    )
    if len(members) < PRIORITY_COHORT_SIZE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"benchmark v{target} rollout requires "
                f"{PRIORITY_COHORT_SIZE} eligible distinct miners"
            ),
        )
    pins: dict = {}
    seed_sources: dict[str, str] = {}
    for member in members:
        candidate, blocker = await qualification_candidate(
            session,
            source_bench_version=from_version,
            target_bench_version=target,
            member=member,
            generator_run_size=generator.run_size,
        )
        if candidate is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cohort agent {member.agent_id} cannot qualify for "
                    f"v{target}: {blocker}"
                ),
            )
        try:
            sha256 = candidate.existing_sha256 or await generator.generate(
                candidate.seed, bench_version=target
            )
        except DataPipelineError as exc:
            raise _generator_unavailable(target, exc) from exc
        pins[member.agent_id] = DatasetPin(
            seed=candidate.seed,
            sha256=sha256,
            run_size=candidate.run_size,
            seed_block=candidate.seed_block,
            seed_block_hash=candidate.seed_block_hash,
        )
        seed_sources[str(member.agent_id)] = candidate.seed_source
    try:
        await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now,
            from_version=from_version,
            desired_version=target,
            rescore_cohort_target=rescore_cohort_target,
            priority_cohort_target=priority_cohort_target,
            audit_context={
                "origin": "admin",
                "actor": payload.actor,
                "reason": payload.reason,
                "from_version": from_version,
                "desired_version": target,
                "seed_sources": seed_sources,
                # Recorded in the immutable audit trail as well as on the row,
                # so the size this rollout was built to is recoverable even if
                # the row is later inspected without its policy history.
                "rescore_cohort_target": rescore_cohort_target,
                "priority_cohort_target": priority_cohort_target,
            },
        )
    except RolloutConflictError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"{exc}. Supersede it first with "
                f"POST /admin/benchmark-rollout/<version>/supersede."
            ),
        ) from exc
    await session.commit()
    return await rollout_state(session, capability_version=target)
