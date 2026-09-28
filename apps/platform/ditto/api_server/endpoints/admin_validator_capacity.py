"""Backroom read of fleet validator capacity (ditto-subnet#2036, telemetry slice).

Every number here is aggregated from reads that already exist: the public
validator fleet view (heartbeat capacity, slot policy, lease reconciliation),
the allocator's shared queue filter, and the live hosted-inference load. This
module adds only the roll-up and the progress-rate estimate.

Read-only by design. The distributed slow lane the issue also asks for is a
scheduling change and is out of scope here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.public import PublicBenchmarkProgress, PublicValidatorHeartbeat
from ditto.api_models.validator_capacity import (
    RelayLaneSaturation,
    ValidatorCapacityAssignment,
    ValidatorCapacityEntry,
    ValidatorCapacitySummary,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import load_validator_fleet
from ditto.api_server.inference_concurrency_settings import settings_from_row
from ditto.db.queries.benchmark_admission import admission_rollout_for_active_version
from ditto.db.queries.inference_concurrency_settings import (
    latest_inference_concurrency_settings_revision,
)
from ditto.db.queries.inference_observability import load_inference_current_rows
from ditto.db.queries.queue_order import unleased_queue_backlog

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Totals always cover the whole live fleet; only the per-validator rows are
# capped, so one call stays small however many hotkeys start reporting.
VALIDATOR_ROW_LIMIT = 64


def _assignment(
    progress: PublicBenchmarkProgress, *, seen_at: datetime, now: datetime
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
    )


def _entry(
    validator: PublicValidatorHeartbeat, *, now: datetime
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
        claimed_slots=len(claimed),
        assignments=[
            _assignment(progress, seen_at=validator.seen_at, now=now)
            for progress in validator.assigned_benchmarks
        ],
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
    fleet = await load_validator_fleet(session, request.app.state, now=now)
    entries = sorted(
        (
            _entry(validator, now=now)
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
    settings = settings_from_row(
        await latest_inference_concurrency_settings_revision(session)
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
        kind = cast(Literal["chat", "embedding"], str(row["request_kind"]))
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
        serviceable_slots=sum(entry.serviceable_slots for entry in entries),
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
