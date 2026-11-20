"""Backroom read of fleet validator capacity (ditto-subnet#2036, telemetry slice).

Every number here is aggregated from reads that already exist: the public
validator fleet view (heartbeat capacity, slot policy, lease reconciliation),
the allocator's shared queue filter, and the live hosted-inference load. This
module adds only the roll-up and the progress-rate estimate.

Read-only by design. The distributed slow lane the issue also asks for is a
scheduling change and is out of scope here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.public import PublicBenchmarkProgress, PublicValidatorHeartbeat
from ditto.api_models.validator_capacity import (
    RelayLaneSaturation,
    SlotUnusableReason,
    ValidatorCapacityAssignment,
    ValidatorCapacityEntry,
    ValidatorCapacitySlot,
    ValidatorCapacitySummary,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import load_validator_fleet
from ditto.api_server.inference_concurrency_settings import settings_from_row
from ditto.api_server.validator_memory_admission import host_memory_admission
from ditto.db.queries.benchmark_admission import admission_rollout_for_active_version
from ditto.db.queries.inference_concurrency_settings import (
    latest_inference_concurrency_settings_revision,
)
from ditto.db.queries.inference_observability import (
    LeaseInferenceActivity,
    LeaseSlotKey,
    load_inference_current_rows,
    load_live_lease_inference_activity,
)
from ditto.db.queries.queue_order import unleased_queue_backlog

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Totals always cover the whole live fleet; only the per-validator rows are
# capped, so one call stays small however many hotkeys start reporting.
VALIDATOR_ROW_LIMIT = 64


def _assignment(
    progress: PublicBenchmarkProgress,
    *,
    seen_at: datetime,
    now: datetime,
    activity: LeaseInferenceActivity | None = None,
) -> ValidatorCapacityAssignment:
    rate: float | None = None
    remaining: float | None = None
    elapsed_minutes = (seen_at - progress.started_at).total_seconds() / 60
    if (
        progress.completed_checks
        and progress.total_checks is not None
        and elapsed_minutes > 0
    ):
        rate = progress.completed_checks / elapsed_minutes
        remaining = (progress.total_checks - progress.completed_checks) / rate
    last_activity = activity.last_activity_at if activity is not None else None
    return ValidatorCapacityAssignment(
        agent_id=progress.agent_id,
        agent_name=progress.agent_name,
        slot_id=progress.slot_id,
        bench_version=progress.bench_version,
        purpose=progress.purpose,
        stage=progress.stage,
        started_at=progress.started_at,
        age_seconds=max(0, int((now - progress.started_at).total_seconds())),
        completed_checks=progress.completed_checks,
        total_checks=progress.total_checks,
        stalled=progress.stalled,
        checks_per_minute=round(rate, 3) if rate is not None else None,
        estimated_remaining_slot_minutes=(
            round(remaining, 1) if remaining is not None else None
        ),
        inference_chat_requests=(
            activity.chat_requests if activity is not None else None
        ),
        inference_embedding_requests=(
            activity.embedding_requests if activity is not None else None
        ),
        inference_last_activity_at=last_activity,
        inference_idle_seconds=(
            max(0, int((now - last_activity).total_seconds()))
            if last_activity is not None
            else None
        ),
    )


def _slot_index(slot_id: str) -> tuple[int, str]:
    """Order ``slot-N`` numerically, anything else after it by name."""
    prefix, _, number = slot_id.rpartition("-")
    if prefix == "slot" and number.isdigit():
        return int(number), ""
    return 1 << 30, slot_id


def _slot_breakdown(
    validator: PublicValidatorHeartbeat,
    *,
    claimed: set[str],
    memory_admitted: int | None,
) -> list[ValidatorCapacitySlot]:
    """Label every configured slot, mirroring the validator's and dispatch's gates.

    The validator offers ``healthy_slots``: none unless it is ``accepting``, and
    at most the memory-admitted count, taken from the lowest slot index up. A
    slot it does not offer is unusable for the validator's reason. Platform
    then funds at most ``allowed_slots`` of the offered slots, holding the last
    ``reserved_round_slots`` of those for generator rounds, and none at all when
    the validator cannot serve the active benchmark or issuance is paused.
    """
    healthy = set(validator.healthy_slots)
    slot_ids = sorted(
        {f"slot-{index}" for index in range(validator.configured_slots)}
        | healthy
        | claimed,
        key=_slot_index,
    )
    offered = [slot for slot in slot_ids if slot in healthy]
    platform_reason: SlotUnusableReason | None = None
    if validator.bench_serviceability == "scorer_unverified":
        platform_reason = "bench_scorer_unverified"
    elif validator.bench_serviceability == "software_obsolete":
        platform_reason = "bench_software_obsolete"
    elif validator.issuance_paused:
        platform_reason = "issuance_paused"
    funded = [] if platform_reason else offered[: validator.allowed_slots]
    reserved = (
        set(funded[len(funded) - validator.reserved_round_slots :])
        if validator.reserved_round_slots
        else set()
    )
    breakdown = []
    for slot_id in slot_ids:
        reason: SlotUnusableReason | None = None
        if slot_id in claimed:
            state = "claimed"
        elif slot_id in reserved:
            state = "reserved_round"
        elif slot_id in funded:
            state = "free"
        else:
            state = "unusable"
            if slot_id in healthy:
                reason = platform_reason or "platform_slot_cap"
            elif validator.admission != "accepting":
                reason = cast(SlotUnusableReason, f"admission_{validator.admission}")
            elif (
                memory_admitted is not None
                and _slot_index(slot_id)[0] >= memory_admitted
            ):
                reason = "host_memory_admission"
            else:
                reason = "validator_unhealthy"
        breakdown.append(
            ValidatorCapacitySlot(
                slot_id=slot_id,
                state=cast(
                    Literal["claimed", "free", "reserved_round", "unusable"], state
                ),
                reason=reason,
            )
        )
    return breakdown


def _entry(
    validator: PublicValidatorHeartbeat,
    *,
    now: datetime,
    activity: Mapping[LeaseSlotKey, LeaseInferenceActivity] | None = None,
    sandbox_memory_limit_mib: int | None = None,
) -> ValidatorCapacityEntry:
    # ``assigned_benchmarks`` is every live lease with its synchronized progress
    # merged in, so a lease the validator has not reported yet still appears
    # (with an unknown rate) instead of reading as free capacity.
    claimed = {
        slot.slot_id
        for slots in (
            validator.assigned_benchmarks,
            validator.active_benchmarks,
            validator.claimed_slots,
            validator.orphaned_slots,
        )
        for slot in slots
    }
    admission = host_memory_admission(
        memory_total_mib=(
            validator.host_specs.memory_total_mib
            if validator.host_specs is not None
            else None
        ),
        configured_slots=validator.configured_slots,
        sandbox_memory_limit_mib=sandbox_memory_limit_mib,
    )
    memory_admitted = admission.admitted_slots if admission is not None else None
    slots = _slot_breakdown(validator, claimed=claimed, memory_admitted=memory_admitted)
    return ValidatorCapacityEntry(
        validator_hotkey=validator.validator_hotkey,
        seen_at=validator.seen_at,
        bench_serviceability=validator.bench_serviceability,
        admission=validator.admission,
        issuance_paused=validator.issuance_paused,
        configured_slots=validator.configured_slots,
        serviceable_slots=(
            min(validator.allowed_slots, len(validator.healthy_slots))
            if validator.bench_serviceability == "serving"
            else 0
        ),
        reserved_round_slots=validator.reserved_round_slots,
        claimed_slots=len(claimed),
        assignments=[
            _assignment(
                progress,
                seen_at=validator.seen_at,
                now=now,
                activity=(activity or {}).get(
                    (validator.validator_hotkey, progress.slot_id, progress.agent_id)
                ),
            )
            for progress in validator.assigned_benchmarks
        ],
        memory_admitted_slots=memory_admitted,
        memory_required_mib=admission.required_mib if admission is not None else None,
        memory_note=admission.note() if admission is not None else None,
        unusable_slots=sum(slot.state == "unusable" for slot in slots),
        slots=slots,
    )


@router.get(
    "/admin/validator-capacity",
    response_model=ValidatorCapacitySummary,
)
async def get_validator_capacity(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> ValidatorCapacitySummary:
    """Serviceable vs claimed slots, assignment progress, queue age, relay load."""
    now = datetime.now(UTC)
    # One read of the live grants serves both the fleet's stall judgement and
    # the per-assignment inference columns, so the two can never disagree.
    activity = await load_live_lease_inference_activity(session, now=now)
    fleet = await load_validator_fleet(
        session, request.app.state, now=now, lease_inference_activity=activity
    )
    settings = settings_from_row(
        await latest_inference_concurrency_settings_revision(session)
    )
    sandbox_memory_limit_mib = settings.benchmark_runtime.sandbox_memory_limit_mib
    entries = sorted(
        (
            _entry(
                validator,
                now=now,
                activity=activity,
                sandbox_memory_limit_mib=sandbox_memory_limit_mib,
            )
            for validator in fleet.validators
            if validator.online
        ),
        key=lambda entry: entry.validator_hotkey,
    )
    assignments = [item for entry in entries for item in entry.assignments]
    eligible_count, oldest_arrival = await unleased_queue_backlog(
        session,
        bench_version=fleet.active_bench_version,
        now=now,
        rollout=await admission_rollout_for_active_version(
            session, bench_version=fleet.active_bench_version
        ),
    )
    global_limits = {
        "chat": settings.chat_global_concurrency,
        "embedding": settings.embedding_global_concurrency,
    }
    inference_config = request.app.state.config.inference_proxy
    relay = []
    for row in await load_inference_current_rows(
        session, stale_after_seconds=inference_config.timeout_seconds * 2
    ):
        kind = cast(Literal["chat", "embedding", "decision"], str(row["request_kind"]))
        active = int(row["active_requests"])
        relay.append(
            RelayLaneSaturation(
                request_kind=kind,
                active_requests=active,
                global_limit=global_limits[kind],
                saturation=round(active / global_limits[kind], 4),
            )
        )
    return ValidatorCapacitySummary(
        generated_at=now,
        active_bench_version=fleet.active_bench_version,
        online_window_seconds=fleet.online_window_seconds,
        live_validator_count=len(entries),
        serviceable_validator_count=sum(
            entry.serviceable_slots > 0 for entry in entries
        ),
        configured_slots=sum(entry.configured_slots for entry in entries),
        # Capped at each host's configured slots: spare memory on one host
        # runs nothing for another.
        memory_admitted_slots=sum(
            min(entry.memory_admitted_slots, entry.configured_slots)
            for entry in entries
            if entry.memory_admitted_slots is not None
        ),
        serviceable_slots=sum(entry.serviceable_slots for entry in entries),
        unusable_slots_by_reason=dict(
            sorted(
                Counter(
                    slot.reason
                    for entry in entries
                    for slot in entry.slots
                    if slot.reason is not None
                ).items()
            )
        ),
        sandbox_memory_limit_mib=sandbox_memory_limit_mib,
        claimed_slots=sum(entry.claimed_slots for entry in entries),
        active_assignment_count=len(assignments),
        estimated_remaining_slot_minutes=round(
            sum(
                item.estimated_remaining_slot_minutes
                for item in assignments
                if item.estimated_remaining_slot_minutes is not None
            ),
            1,
        ),
        unestimated_assignment_count=sum(
            item.estimated_remaining_slot_minutes is None for item in assignments
        ),
        eligible_unleased_count=eligible_count,
        oldest_eligible_unleased_age_seconds=(
            max(0, int((now - oldest_arrival).total_seconds()))
            if oldest_arrival is not None
            else None
        ),
        relay=relay,
        validators=entries[:VALIDATOR_ROW_LIMIT],
        validators_truncated=len(entries) > VALIDATOR_ROW_LIMIT,
    )
