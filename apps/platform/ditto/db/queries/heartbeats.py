"""Persistence for signed validator software heartbeats."""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    BenchmarkProgressStage,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.db.models import (
    Agent,
    ScreenerHeartbeat,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.metrics import VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


# Every member of ``BenchmarkProgressStage`` must appear here. ``_STAGE_ORDER`` is
# subscripted directly by :func:`_validate_same_lease_progress`, so a missing
# stage raises ``KeyError`` — not ``HeartbeatProgressRegressionError`` — which no
# call site catches. That escapes as a 500 from the heartbeat ingest, freezing
# ``seen_at`` and making an actively scoring validator read as heartbeat_stale:
# exactly the "stuck stream looks like a stuck run" confusion this module is
# supposed to resolve. ``generating_dataset`` was added to the wire enum without
# being added here. mypy cannot catch it (a ``dict[Literal, int]`` literal need
# not be exhaustive), so ``test_stage_order_covers_every_wire_stage`` does.
_STAGE_ORDER: dict[BenchmarkProgressStage, int] = {
    "preparing": 0,
    "building_harness": 1,
    "generating_dataset": 2,
    "starting_harness": 3,
    "running_benchmark": 4,
    # A relay pause is a reversible sub-state of the running stage.
    "waiting_for_relay": 4,
    "finalizing": 5,
    "submitting_result": 6,
    "failed_retrying": 7,
}


class HeartbeatProgressRegressionError(ValueError):
    """Raised when a newer heartbeat regresses progress for the same lease."""


@dataclass(frozen=True)
class ActiveValidatorWork:
    """One ticket-validated active heartbeat used by every public projection."""

    heartbeat: ValidatorHeartbeat
    ticket: ValidatorTicket
    agent: Agent
    progress: BenchmarkProgress | None


@dataclass(frozen=True)
class ActiveValidatorAssignment:
    """One live platform-issued validator assignment, independent of heartbeat."""

    ticket: ValidatorTicket
    agent: Agent


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _raw_percent(progress: BenchmarkProgress) -> int | None:
    if progress.completed is None or progress.total is None:
        return None
    return progress.completed * 100 // progress.total


def _parse_progress(value: dict) -> BenchmarkProgress:
    """Validate a JSON-column value through Pydantic's strict JSON path."""
    return BenchmarkProgress.model_validate_json(json.dumps(value))


def _is_same_run(previous: BenchmarkProgress, current: BenchmarkProgress) -> bool:
    """Whether two progress reports describe the same dittobench run.

    Runs are keyed on the opaque ``run_token``: all heartbeats for one run carry
    the same token, and a retry or the next confirmation seed carries a fresh
    one. When the token changes the run is new, so monotonicity must NOT be
    enforced across the boundary (the fresh run legitimately restarts its
    counts). Two ``None`` tokens (old validators, or the token-less preparing
    heartbeat emitted before a run_id exists) compare equal and are treated as
    the same run, preserving the pre-token monotonicity behaviour.
    """
    return previous.run_token == current.run_token


def _validate_same_lease_progress(
    previous: BenchmarkProgress | None, current: BenchmarkProgress | None
) -> None:
    if previous is None or current is None:
        # A v16 reporter announces a leased slot before it has progress to send,
        # so one side being absent is the claim "nothing to compare yet", not a
        # regression. There is no ordering to enforce against a null.
        return
    if previous.ticket_deadline != current.ticket_deadline:
        return
    # `preparing` is the first stage of every run (the scorer's queued -> preparing),
    # so a heartbeat that reports it marks a fresh run WITHIN the same lease and its
    # progress legitimately resets. This covers both a failed_retrying retry and the
    # next confirmation seed of a multi-seed evaluation (a completed run at
    # finalizing/submitting_result followed by preparing for the next seed). Accept
    # it and rebaseline; monotonicity is still enforced within a single run below.
    # Without this, the reset heartbeat is rejected, the baseline stays pinned to the
    # prior run's high completed count, and every subsequent heartbeat is refused
    # ("completed count cannot regress") — freezing seen_at so a healthy, actively
    # scoring validator reads as heartbeat_stale.
    if current.stage == "preparing":
        return
    if _STAGE_ORDER[current.stage] < _STAGE_ORDER[previous.stage]:
        raise HeartbeatProgressRegressionError(
            "benchmark stage cannot regress for the same ticket lease"
        )
    if previous.total is not None and current.total != previous.total:
        raise HeartbeatProgressRegressionError(
            "benchmark total cannot change for the same ticket lease"
        )
    if previous.completed is not None and (
        current.completed is None or current.completed < previous.completed
    ):
        raise HeartbeatProgressRegressionError(
            "benchmark completed count cannot regress for the same ticket lease"
        )
    previous_percent = _raw_percent(previous)
    current_percent = _raw_percent(current)
    if previous_percent is not None and (
        current_percent is None or current_percent < previous_percent
    ):
        raise HeartbeatProgressRegressionError(
            "benchmark percent cannot regress for the same ticket lease"
        )


def _reconcile_capacity_progress(
    previous: dict | None, incoming: dict | None
) -> dict | None:
    """Keep every same-lease slot monotonic while holding the heartbeat row lock."""
    if not isinstance(previous, dict) or not isinstance(incoming, dict):
        return incoming
    try:
        previous_capacity = BenchmarkCapacity.model_validate(previous)
        incoming_capacity = BenchmarkCapacity.model_validate(incoming)
    except ValidationError:
        return incoming
    previous_slots = {slot.slot_id: slot for slot in previous_capacity.active}
    reconciled = []
    for slot in incoming_capacity.active:
        prior = previous_slots.get(slot.slot_id)
        if prior is not None and prior.agent_id == slot.agent_id:
            try:
                _validate_same_lease_progress(prior.progress, slot.progress)
            except HeartbeatProgressRegressionError:
                slot = prior
        reconciled.append(slot)
    return incoming_capacity.model_copy(update={"active": reconciled}).model_dump(
        mode="json"
    )


def _regresses_within_run(
    row: ValidatorHeartbeat,
    *,
    benchmark_progress: dict | None,
    active_agent_id: UUID | None,
    validator_hotkey: str,
) -> bool:
    """Whether the incoming progress walks the stored progress backwards.

    True means "keep what is stored as the public display floor". This is a pure
    read over already-loaded values so callers can guard it without a savepoint.
    """
    if row.benchmark_progress is None or benchmark_progress is None:
        return False
    try:
        previous_progress = _parse_progress(row.benchmark_progress)
        current_progress = _parse_progress(benchmark_progress)
    except ValidationError:
        # A malformed stored or incoming blob is not a reason to reject an
        # authenticated liveness report. Fail open: skip monotonicity and
        # accept the incoming progress.
        return False
    if row.benchmark_progress_agent_id != active_agent_id or not _is_same_run(
        previous_progress, current_progress
    ):
        # A changed lease or run_token rebaselines: the fresh run restarts its
        # counts legitimately, so monotonicity does not apply across the boundary.
        return False
    # Same lease, same run: enforce monotonicity, but fail open on a regression.
    try:
        _validate_same_lease_progress(previous_progress, current_progress)
    except HeartbeatProgressRegressionError:
        logger.info(
            "validator heartbeat kept prior progress after regression "
            "validator=%s stored_stage=%s incoming_stage=%s",
            validator_hotkey,
            previous_progress.stage,
            current_progress.stage,
        )
        return True
    return False


async def upsert_validator_heartbeat(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    software_version: str,
    protocol_version: int,
    code_digest: str,
    state: str,
    active_agent_id: UUID | None,
    system_metrics: dict | None,
    benchmark_progress: dict | None,
    reported_at: datetime,
    seen_at: datetime,
    signature: str,
    capabilities: dict | None = None,
    stack: dict | None = None,
    stack_health: dict | None = None,
    benchmark_capacity: dict | None = None,
    claimed_slots: list[dict] | None = None,
) -> tuple[ValidatorHeartbeat, bool]:
    """Persist only a strictly newer heartbeat; return ``(row, accepted)``."""
    row = await session.scalar(
        select(ValidatorHeartbeat)
        .where(ValidatorHeartbeat.validator_hotkey == validator_hotkey)
        .with_for_update()
    )
    is_new = False
    if row is None:
        values = {
            "validator_hotkey": validator_hotkey,
            "software_version": software_version,
            "protocol_version": protocol_version,
            "code_digest": code_digest,
            "state": state,
            "active_agent_id": active_agent_id,
            "first_seen_at": seen_at,
            "system_metrics": system_metrics,
            "benchmark_progress": benchmark_progress,
            "benchmark_progress_reported": benchmark_progress is not None,
            "benchmark_progress_agent_id": (
                active_agent_id if benchmark_progress is not None else None
            ),
            "capabilities": capabilities,
            "stack": stack,
            "stack_health": stack_health,
            "benchmark_capacity": benchmark_capacity,
            "claimed_slots": claimed_slots,
            "reported_at": reported_at,
            "seen_at": seen_at,
            "signature": signature,
        }
        dialect_name = session.get_bind().dialect.name
        inserted: str | None = None
        if dialect_name == "postgresql":
            statement = (
                postgresql_insert(ValidatorHeartbeat)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["validator_hotkey"])
                .returning(ValidatorHeartbeat.validator_hotkey)
            )
            inserted = await session.scalar(statement)
        elif dialect_name == "sqlite":
            sqlite_statement = (
                sqlite_insert(ValidatorHeartbeat)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["validator_hotkey"])
                .returning(ValidatorHeartbeat.validator_hotkey)
            )
            inserted = await session.scalar(sqlite_statement)
        if dialect_name in {"postgresql", "sqlite"}:
            if inserted is not None:
                row = await session.get(ValidatorHeartbeat, validator_hotkey)
                assert row is not None
                return row, True
            row = await session.scalar(
                select(ValidatorHeartbeat)
                .where(ValidatorHeartbeat.validator_hotkey == validator_hotkey)
                .with_for_update()
            )
        if row is None:
            row = ValidatorHeartbeat(
                validator_hotkey=validator_hotkey, first_seen_at=seen_at
            )
            session.add(row)
            is_new = True
    # When True, a regression was detected within one run: keep the previously
    # stored progress as the public display floor instead of moving it backward,
    # but still persist the fresh liveness/telemetry below (fail-open).
    keep_stored_progress = False
    if not is_new:
        assert row is not None
        existing_reported_at = row.reported_at
        if existing_reported_at.tzinfo is None:
            existing_reported_at = existing_reported_at.replace(tzinfo=UTC)
        if reported_at <= existing_reported_at:
            return row, False
        # Payload reasoning, not liveness. It runs on data already in memory, so
        # a failure here cannot poison the transaction — but it must not escape
        # either, because everything below it is the liveness write.
        try:
            keep_stored_progress = _regresses_within_run(
                row,
                benchmark_progress=benchmark_progress,
                active_agent_id=active_agent_id,
                validator_hotkey=validator_hotkey,
            )
        except Exception as error:  # noqa: BLE001 - liveness must not depend on payload
            keep_stored_progress = False
            VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED.labels(
                stage="progress_monotonicity", reason=type(error).__name__
            ).inc()
            logger.exception(
                "validator heartbeat skipped progress monotonicity after an "
                "unexpected failure validator=%s",
                validator_hotkey,
            )
    row.software_version = software_version
    row.protocol_version = protocol_version
    row.code_digest = code_digest
    row.state = state
    row.active_agent_id = active_agent_id
    row.system_metrics = system_metrics
    row.capabilities = capabilities
    row.stack = stack
    row.stack_health = stack_health
    try:
        row.benchmark_capacity = _reconcile_capacity_progress(
            row.benchmark_capacity if not is_new else None, benchmark_capacity
        )
    except Exception as error:  # noqa: BLE001 - liveness must not depend on payload
        # Reconciliation only smooths per-slot display; the incoming capacity was
        # already ticket-validated by the caller. Store it unreconciled rather
        # than losing the liveness write this assignment sits inside.
        row.benchmark_capacity = benchmark_capacity
        VALIDATOR_HEARTBEAT_PAYLOAD_DEGRADED.labels(
            stage="capacity_reconcile", reason=type(error).__name__
        ).inc()
        logger.exception(
            "validator heartbeat stored unreconciled capacity after an "
            "unexpected failure validator=%s",
            validator_hotkey,
        )
    if benchmark_progress is not None and not keep_stored_progress:
        row.benchmark_progress = benchmark_progress
        row.benchmark_progress_reported = True
        row.benchmark_progress_agent_id = active_agent_id
    elif benchmark_progress is not None and keep_stored_progress:
        # Fail-open regression: retain the stored progress and its agent binding
        # (never move the public display backward) while still marking it
        # reported so the live lease keeps showing the last good progress.
        row.benchmark_progress_reported = True
    else:
        # Retain the last signed progress and its separate agent binding as a
        # private monotonic floor across idle/polling/downgrade heartbeats. The
        # public view follows this flag and therefore clears immediately.
        row.benchmark_progress_reported = False
    # Straight off the verified signature: no reconciliation, no monotonicity.
    # This is the validator's own statement about which slots it is busy on, and
    # it is only ever read to REFUSE a revocation.
    row.claimed_slots = claimed_slots
    row.reported_at = reported_at
    row.seen_at = seen_at
    row.signature = signature
    await session.flush()
    return row, True


async def list_validator_heartbeats(
    session: AsyncSession,
) -> list[ValidatorHeartbeat]:
    """Return every reporting validator, newest heartbeat first."""
    result = await session.scalars(
        select(ValidatorHeartbeat).order_by(
            ValidatorHeartbeat.seen_at.desc(), ValidatorHeartbeat.validator_hotkey
        )
    )
    return list(result)


async def count_live_validators(
    session: AsyncSession,
    *,
    now: datetime,
    freshness: timedelta = timedelta(minutes=15),
) -> int:
    """How many validators have heartbeated recently enough to be folding weights.

    Deliberately capability-agnostic, unlike
    :func:`live_validator_fleet_supports_protocol`: every registered validator
    submits weights regardless of which benchmark versions it can score, so for
    "is anyone out there applying this policy" the capable subset is the wrong
    denominator.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(ValidatorHeartbeat)
            .where(ValidatorHeartbeat.seen_at >= now - freshness)
        )
    ) or 0


async def live_validator_fleet_supports_protocol(
    session: AsyncSession,
    *,
    minimum_protocol: int,
    bench_version: int,
    now: datetime,
    freshness: timedelta = timedelta(minutes=15),
) -> bool:
    """Whether every recently-live benchmark-capable validator supports a contract.

    Readiness is global within the fleet that can actually score ``bench_version``.
    Legacy validators and unrelated scorer stacks cannot consume its continual
    retest work and therefore must not hold its aggregate fold inactive. An empty
    capable fleet still fails closed, and every capable member must meet the
    protocol floor so compatible validators receive byte-equivalent semantics.
    """
    heartbeats = list(
        await session.scalars(
            select(ValidatorHeartbeat).where(
                ValidatorHeartbeat.seen_at >= now - freshness
            )
        )
    )
    protocols: list[int] = []
    for heartbeat in heartbeats:
        try:
            capabilities = ValidatorCapabilities.model_validate_json(
                json.dumps(heartbeat.capabilities)
            )
        except ValidationError:
            continue
        scorer = capabilities.scorer_benchmarks
        if (
            scorer is None
            or scorer.status != "fresh_verified"
            or bench_version not in scorer.supported_bench_versions
        ):
            continue
        protocols.append(heartbeat.protocol_version)
    return bool(protocols) and min(protocols) >= minimum_protocol


async def list_active_validator_work(
    session: AsyncSession,
    *,
    now: datetime,
    cutoff: datetime,
    agent_id: UUID | None = None,
) -> list[ActiveValidatorWork]:
    """Return every fresh heartbeat slot still bound to a live ticket.

    Protocol v10+ stores an ingest-validated capacity payload containing every
    active slot. ``active_agent_id`` mirrors only the first slot for legacy
    consumers, so treating it as the whole heartbeat hides concurrent work from
    activity and pipeline projections. Match every capacity slot back to its live
    ticket identity here. Legacy v2-v9 rows retain their deadline-bound scalar
    progress validation so a stale heartbeat cannot revive after a requeue.
    """
    heartbeat_rows = list(
        await session.scalars(
            select(ValidatorHeartbeat)
            .where(
                ValidatorHeartbeat.state == "running_benchmark",
                ValidatorHeartbeat.seen_at >= cutoff,
            )
            .order_by(ValidatorHeartbeat.validator_hotkey)
        )
    )
    if not heartbeat_rows:
        return []

    hotkeys = [heartbeat.validator_hotkey for heartbeat in heartbeat_rows]
    assignment_statement = (
        select(ValidatorTicket, Agent)
        .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.validator_hotkey.in_(hotkeys),
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
            Agent.status.in_(SCOREABLE_AGENT_STATUSES),
        )
    )
    if agent_id is not None:
        assignment_statement = assignment_statement.where(Agent.agent_id == agent_id)
    assignments = (await session.execute(assignment_statement)).all()
    assignments_by_identity = {
        (ticket.validator_hotkey, ticket.slot_id, ticket.agent_id): (ticket, agent)
        for ticket, agent in assignments
    }

    active: list[ActiveValidatorWork] = []
    for heartbeat in heartbeat_rows:
        capacity: BenchmarkCapacity | None = None
        if heartbeat.protocol_version >= 10:
            with contextlib.suppress(ValidationError):
                capacity = BenchmarkCapacity.model_validate(
                    heartbeat.benchmark_capacity
                )
        if capacity is not None:
            for slot in sorted(capacity.active, key=lambda item: item.slot_id):
                assignment = assignments_by_identity.get(
                    (heartbeat.validator_hotkey, slot.slot_id, slot.agent_id)
                )
                if assignment is None:
                    continue
                ticket, agent = assignment
                if ticket.bench_version != slot.bench_version:
                    continue
                active.append(
                    ActiveValidatorWork(
                        heartbeat=heartbeat,
                        ticket=ticket,
                        agent=agent,
                        progress=slot.progress,
                    )
                )
            continue

        if heartbeat.active_agent_id is None:
            continue
        assignment = assignments_by_identity.get(
            (
                heartbeat.validator_hotkey,
                "slot-0",
                heartbeat.active_agent_id,
            )
        )
        if assignment is None:
            continue
        ticket, agent = assignment
        progress: BenchmarkProgress | None = None
        if heartbeat.protocol_version >= 4:
            if not heartbeat.benchmark_progress_reported:
                if _aware(heartbeat.reported_at) <= _aware(ticket.issued_at).replace(
                    microsecond=0
                ):
                    continue
            else:
                if heartbeat.benchmark_progress is None:
                    continue
                try:
                    progress = _parse_progress(heartbeat.benchmark_progress)
                except ValidationError:
                    continue
                if progress.ticket_deadline != _aware(ticket.deadline):
                    continue
        elif _aware(heartbeat.reported_at) <= _aware(ticket.issued_at).replace(
            microsecond=0
        ):
            continue
        active.append(
            ActiveValidatorWork(
                heartbeat=heartbeat,
                ticket=ticket,
                agent=agent,
                progress=progress,
            )
        )
    return active


async def list_active_validator_assignments(
    session: AsyncSession,
    *,
    now: datetime,
    agent_id: UUID | None = None,
) -> list[ActiveValidatorAssignment]:
    """Return platform assignment truth without inferring validator liveness."""
    statement = (
        select(ValidatorTicket, Agent)
        .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .order_by(ValidatorTicket.validator_hotkey)
    )
    if agent_id is not None:
        statement = statement.where(ValidatorTicket.agent_id == agent_id)
    rows = (await session.execute(statement)).all()
    return [
        ActiveValidatorAssignment(ticket=ticket, agent=agent) for ticket, agent in rows
    ]


async def upsert_screener_heartbeat(
    session: AsyncSession,
    *,
    screener_hotkey: str,
    instance_id: str,
    software_version: str,
    protocol_version: int,
    policy_version: int,
    state: str,
    active_agent_id: UUID | None,
    screening_progress: dict | None,
    system_metrics: dict | None,
    review_settings: dict | None,
    reported_at: datetime,
    seen_at: datetime,
    signature: str,
) -> tuple[ScreenerHeartbeat, bool]:
    """Persist only a strictly newer heartbeat for one (hotkey, instance)."""
    row = await session.get(ScreenerHeartbeat, (screener_hotkey, instance_id))
    if row is None:
        row = ScreenerHeartbeat(
            screener_hotkey=screener_hotkey,
            instance_id=instance_id,
            first_seen_at=seen_at,
        )
        session.add(row)
    else:
        existing_reported_at = row.reported_at
        if existing_reported_at.tzinfo is None:
            existing_reported_at = existing_reported_at.replace(tzinfo=UTC)
        if reported_at <= existing_reported_at:
            return row, False
    row.software_version = software_version
    row.protocol_version = protocol_version
    row.policy_version = policy_version
    row.state = state
    row.active_agent_id = active_agent_id
    # Reuse the existing JSON telemetry column. Legacy rows contain the raw
    # metrics object; active v2 rows use this private envelope so no migration is
    # needed and public projection still reconstructs fields from an allowlist.
    row.system_metrics = (
        {
            "system_metrics": system_metrics,
            "screening_progress": screening_progress,
            "review_settings": review_settings,
        }
        if screening_progress is not None or review_settings is not None
        else system_metrics
    )
    row.reported_at = reported_at
    row.seen_at = seen_at
    row.signature = signature
    await session.flush()
    return row, True


async def list_screener_heartbeats(
    session: AsyncSession,
) -> list[ScreenerHeartbeat]:
    """Return every reporting screener instance, newest heartbeat first."""
    result = await session.scalars(
        select(ScreenerHeartbeat).order_by(
            ScreenerHeartbeat.seen_at.desc(),
            ScreenerHeartbeat.screener_hotkey,
            ScreenerHeartbeat.instance_id,
        )
    )
    return list(result)


async def prune_stale_screener_heartbeats(
    session: AsyncSession,
    *,
    before: datetime,
) -> None:
    """Delete heartbeat rows last seen before ``before``.

    Bounds the per-instance table: a scaled-in fleet instance (unique name)
    stops reporting and would otherwise leave a permanent dead row.
    """
    await session.execute(
        delete(ScreenerHeartbeat).where(ScreenerHeartbeat.seen_at < before),
        execution_options={"synchronize_session": False},
    )
