"""Audited singleton benchmark diagnostics, never rollout or score authority."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_canary import (
    BenchmarkCanaryCancel,
    BenchmarkCanaryIssue,
    BenchmarkCanaryView,
)
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import ValidatorCapabilities
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.validator import (
    _TICKET_TTL,
    ChainDep,
    GeneratorDep,
    SessionDep,
    _assert_validator_compatible,
    _assert_validator_permitted,
    _heartbeat_resource_sample,
    _held_lease_slots,
    _inference_stage_slot_cap,
    _validator_slot_settings,
)
from ditto.api_server.inference_concurrency_settings import resolved_proxy_config
from ditto.api_server.onchain_seed import derive_validator_seed
from ditto.api_server.validator_slot_settings import (
    allowed_slot_count,
    validator_issuance_paused,
)
from ditto.db.models import (
    Agent,
    BenchmarkCanary,
    Score,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.audit import append_audit_entry
from ditto.db.queries.benchmark_rollout import (
    MIN_SCOREABLE_BENCH_VERSION,
    active_bench_version,
    heartbeat_supports_version,
)
from ditto.db.queries.inference import ensure_inference_grant, revoke_ticket_inference
from ditto.db.queries.provider_outages import lock_provider_work_gate, scoring_probe_key
from ditto.db.queries.validator_slot_occupancy import (
    live_ordinary_slot_ticket,
    live_v9_confirmation_slot_ticket,
    lock_validator_slot,
)

router = APIRouter(prefix="/admin/benchmark-canaries", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


def _view(row: BenchmarkCanary) -> BenchmarkCanaryView:
    view = BenchmarkCanaryView.model_validate(row)
    if view.status == "issued" and view.deadline <= datetime.now(UTC):
        view.status = "expired"
    return view


@router.get("", response_model=list[BenchmarkCanaryView])
async def list_benchmark_canaries(
    _: AdminDep,
    session: SessionDep,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[BenchmarkCanaryView]:
    response.headers["Cache-Control"] = "no-store"
    rows = await session.scalars(
        select(BenchmarkCanary)
        .order_by(BenchmarkCanary.issued_at.desc(), BenchmarkCanary.canary_id)
        .limit(limit)
        .offset(offset)
    )
    return [_view(row) for row in rows]


@router.get("/{canary_id}", response_model=BenchmarkCanaryView)
async def get_benchmark_canary(
    canary_id: UUID,
    _: AdminDep,
    session: SessionDep,
    response: Response,
) -> BenchmarkCanaryView:
    response.headers["Cache-Control"] = "no-store"
    row = await session.get(BenchmarkCanary, canary_id)
    if row is None:
        raise HTTPException(404, "canary not found")
    return _view(row)


@router.post("/{canary_id}/cancel", response_model=BenchmarkCanaryView)
async def cancel_benchmark_canary(
    canary_id: UUID,
    payload: BenchmarkCanaryCancel,
    _: AdminDep,
    session: SessionDep,
    response: Response,
) -> BenchmarkCanaryView:
    response.headers["Cache-Control"] = "no-store"
    if payload.confirmation != f"CANCEL CANARY {canary_id}":
        raise HTTPException(422, "exact canary cancellation confirmation required")
    async with session.begin():
        snapshot = await session.get(BenchmarkCanary, canary_id)
        if snapshot is None:
            raise HTTPException(404, "canary not found")
        ticket = await session.get(
            ValidatorTicket,
            (snapshot.agent_id, snapshot.bench_version, snapshot.validator_hotkey),
            with_for_update=True,
        )
        await session.refresh(snapshot, with_for_update=True)
        if snapshot.status != "issued":
            return _view(snapshot)
        if (
            ticket is None
            or ticket.purpose != TicketPurpose.BENCHMARK_CANARY
            or ticket.deadline != snapshot.deadline
        ):
            raise HTTPException(409, "canary no longer owns this ticket")
        now = datetime.now(UTC)
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        ticket.status = TicketStatus.EXPIRED
        ticket.retry_after = None
        snapshot.status = "cancelled"
        snapshot.finished_at = now
        snapshot.failure_detail = payload.reason
        await append_audit_entry(
            session,
            agent_id=snapshot.agent_id,
            validator_hotkey=snapshot.validator_hotkey,
            event="benchmark_canary_cancelled",
            payload={
                "canary_id": str(canary_id),
                "actor": payload.actor,
                "reason": payload.reason,
                "authoritative": False,
            },
            recorded_at=now,
        )
        return _view(snapshot)


@router.post("", response_model=BenchmarkCanaryView)
async def issue_benchmark_canary(
    payload: BenchmarkCanaryIssue,
    _: AdminDep,
    session: SessionDep,
    generator: GeneratorDep,
    chain: ChainDep,
    request: Request,
    response: Response,
) -> BenchmarkCanaryView:
    """Reserve one existing protocol lease, with a separate diagnostic receipt.

    No mutable BenchmarkDataset, Score, Agent or Rollout fields are written.
    Reject existing ticket identities rather than replacing any production work.
    """
    response.headers["Cache-Control"] = "no-store"
    if (
        payload.confirmation
        != f"ISSUE CANARY V{payload.bench_version} {payload.agent_id}"
    ):
        raise HTTPException(422, "exact canary confirmation required")
    try:
        contract = benchmark_contract(payload.bench_version)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.bench_version < MIN_SCOREABLE_BENCH_VERSION:
        raise HTTPException(409, "retired benchmark versions cannot receive leases")
    if generator.run_size != "full":
        raise HTTPException(
            409, "canary requires the full production generator profile"
        )
    slots = await _validator_slot_settings(request)
    inference_config = await resolved_proxy_config(
        request.app.state, request.app.state.config.inference_proxy
    )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    async with session.begin():
        # One fleet-wide diagnostic at a time and race-free idempotency. Normal
        # allocation uses the slot lock below and is not globally serialized.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended("benchmark-canary-singleton", 0)
                )
            )
        )
        existing = await session.get(BenchmarkCanary, payload.canary_id)
        if existing is not None:
            for field, expected in {
                "agent_id": payload.agent_id,
                "bench_version": payload.bench_version,
                "validator_hotkey": payload.validator_hotkey,
                "slot_id": payload.slot_id,
                "artifact_sha256": payload.expected_artifact_sha256,
                "screened_image_sha256": payload.expected_screened_image_sha256,
                "actor": payload.actor,
                "reason": payload.reason,
            }.items():
                if getattr(existing, field) != expected:
                    raise HTTPException(409, "canary id already binds different inputs")
            return _view(existing)
        now = datetime.now(UTC)
        if await active_bench_version(session) != payload.expected_active_version:
            raise HTTPException(409, "active benchmark changed")
        if (
            await session.scalar(
                select(BenchmarkCanary.canary_id)
                .where(
                    BenchmarkCanary.status == "issued",
                    BenchmarkCanary.deadline > now,
                )
                .limit(1)
            )
            is not None
        ):
            raise HTTPException(409, "another canary is already in flight")
        # Match normal dispatch's lock order: provider gate -> slot -> agent.
        # Taking the provider lock after a slot can deadlock a concurrent poll
        # that already holds the gate and is waiting for that same slot.
        gate = await lock_provider_work_gate(
            session,
            now=now,
            kind="scoring",
            key=scoring_probe_key(
                validator_hotkey=payload.validator_hotkey, slot_id=payload.slot_id
            ),
        )
        if not gate.admitted or (
            gate.circuit is not None and gate.circuit.state == "open"
        ):
            raise HTTPException(409, "provider work gate is not healthy")
        await lock_validator_slot(
            session, validator_hotkey=payload.validator_hotkey, slot_id=payload.slot_id
        )
        await _assert_validator_compatible(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            config=request.app.state.config.validator_compatibility,
        )
        heartbeat = await session.get(ValidatorHeartbeat, payload.validator_hotkey)
        if heartbeat is None or not heartbeat_supports_version(
            heartbeat, now=now, version=payload.bench_version
        ):
            raise HTTPException(409, "validator lacks fresh target-version capability")
        try:
            capacity = BenchmarkCapacity.model_validate(heartbeat.benchmark_capacity)
            capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
        except ValidationError as exc:
            raise HTTPException(
                409, "validator capacity or capabilities are invalid"
            ) from exc
        if not capabilities.ticket_inference or heartbeat.protocol_version < 11:
            raise HTTPException(409, "validator lacks ticket inference capability")
        held = await _held_lease_slots(
            session, validator_hotkey=payload.validator_hotkey, now=now
        )
        cap = min(
            allowed_slot_count(
                slots,
                advertised_slots=capacity.configured_slots,
                sample=_heartbeat_resource_sample(heartbeat),
            ),
            _inference_stage_slot_cap(inference_config),
        )
        if (
            validator_issuance_paused(slots, validator_hotkey=payload.validator_hotkey)
            or capacity.admission != "accepting"
            or payload.slot_id not in capacity.healthy_slots
            or any(slot.slot_id == payload.slot_id for slot in capacity.active)
            or len(held) >= cap
        ):
            raise HTTPException(
                409, "validator slot is not available under current policy"
            )
        for lookup in (live_ordinary_slot_ticket, live_v9_confirmation_slot_ticket):
            if (
                await lookup(
                    session,
                    validator_hotkey=payload.validator_hotkey,
                    slot_id=payload.slot_id,
                    now=now,
                )
                is not None
            ):
                raise HTTPException(409, "validator slot already holds a lease")
        key = (payload.agent_id, payload.bench_version, payload.validator_hotkey)
        if (
            await session.scalar(
                select(ValidatorTicket.agent_id).where(
                    ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                    ValidatorTicket.slot_id == payload.slot_id,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                )
            )
            is not None
        ):
            raise HTTPException(
                409, "slot has an unswept lease; wait for normal expiry cleanup"
            )
        if await session.get(ValidatorTicket, key, with_for_update=True) is not None:
            raise HTTPException(
                409, "ticket identity already exists; choose another validator"
            )
        if await session.get(Score, key) is not None:
            raise HTTPException(
                409, "validator already has a canonical score for this target"
            )
        agent = await session.get(Agent, payload.agent_id, with_for_update=True)
        if (
            agent is None
            or agent.status not in {AgentStatus.SCORED, AgentStatus.LIVE}
            or agent.sha256 != payload.expected_artifact_sha256
            or agent.screened_image_sha256 != payload.expected_screened_image_sha256
            or not agent.screened_image_verified_at
            or not agent.screened_image_upload_id
            or not agent.screened_image_id
            or not agent.screened_image_ref
            or not agent.screened_image_size_bytes
            or (agent.screening_policy_version or 0)
            < contract.minimum_screening_policy_version
            or agent.dataset_seed_block is None
            or not agent.dataset_seed_block_hash
        ):
            raise HTTPException(
                409, "agent is not an eligible immutable screened canary target"
            )
        seed = derive_validator_seed(
            agent.dataset_seed_block_hash, agent.agent_id, payload.validator_hotkey
        )
        digest = await generator.generate(seed, bench_version=payload.bench_version)
        if digest is None:
            raise HTTPException(503, "generator did not return a dataset digest")
        if await active_bench_version(session) != payload.expected_active_version:
            raise HTTPException(409, "active benchmark changed during preparation")
        now = datetime.now(UTC)
        ticket = ValidatorTicket(
            agent_id=agent.agent_id,
            bench_version=payload.bench_version,
            validator_hotkey=payload.validator_hotkey,
            slot_id=payload.slot_id,
            status=TicketStatus.ISSUED,
            purpose=TicketPurpose.BENCHMARK_CANARY,
            purpose_revision=1,
            legacy_completion_allowed=False,
            issued_at=now,
            deadline=now + _TICKET_TTL,
            seed=seed,
            dataset_sha256=digest,
            seed_block=agent.dataset_seed_block,
            seed_block_hash=agent.dataset_seed_block_hash,
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add(ticket)
        await session.flush()
        calibration = (
            capabilities.scorer_benchmarks.v7_calibration
            if capabilities.scorer_benchmarks is not None
            else None
        )
        grant = await ensure_inference_grant(
            session,
            ticket=ticket,
            config=inference_config,
            supported_profiles=(
                tuple(route.profile_revision for route in calibration.supported_routes)
                if calibration is not None
                else None
            ),
            calibration_manifest_sha256=(
                calibration.manifest_sha256 if calibration is not None else None
            ),
        )
        if grant is None:
            raise HTTPException(409, "target-version inference route is not ready")
        canary = BenchmarkCanary(
            canary_id=payload.canary_id,
            agent_id=agent.agent_id,
            bench_version=payload.bench_version,
            validator_hotkey=payload.validator_hotkey,
            slot_id=payload.slot_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256,
            seed=seed,
            dataset_sha256=digest,
            run_size="full",
            actor=payload.actor,
            reason=payload.reason,
            issued_at=now,
            deadline=ticket.deadline,
            status="issued",
        )
        session.add(canary)
        await append_audit_entry(
            session,
            agent_id=agent.agent_id,
            validator_hotkey=payload.validator_hotkey,
            event="benchmark_canary_issued",
            recorded_at=now,
            payload={
                "canary_id": str(canary.canary_id),
                "bench_version": payload.bench_version,
                "actor": payload.actor,
                "reason": payload.reason,
                "authoritative": False,
                "artifact_sha256": agent.sha256,
                "dataset_sha256": digest,
            },
        )
        await session.flush()
        result = BenchmarkCanaryView.model_validate(canary)
    return result
