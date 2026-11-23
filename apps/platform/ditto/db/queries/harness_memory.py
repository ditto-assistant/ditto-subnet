"""Per-run harness memory observations and their per-agent rollup (SN-500).

Observability only: nothing here feeds dispatch or scoring. Operators read
every field through the admin routes; each agent's own runs are also on its
public pipeline, without failure reason or detail. Rows arrive unsigned from
validators with the score or the failed-ticket hand-back; there is no history
before this table existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Float, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.validator import HarnessMemoryTelemetry
from ditto.db.models import HarnessMemoryObservation

logger = logging.getLogger(__name__)

HarnessMemoryOutcome = Literal["scored", "failed"]

# Failure details that mean the harness hit its own memory cap: the charged
# verdict and the notice-period no-fault one.
_OOM_DETAILS = ("harness_oom", "harness_oom_grace")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def record_harness_memory(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
    lease_issued_at: datetime,
    outcome: HarnessMemoryOutcome,
    telemetry: HarnessMemoryTelemetry,
    failure_reason: str | None = None,
    failure_detail: str | None = None,
) -> None:
    """Upsert one lease's observation; never raises into the caller.

    Runs in a savepoint so a telemetry write can never fail the score or the
    hand-back it rides with. Keyed on the lease, so a replayed report
    overwrites rather than double-counts.
    """
    values = {
        "agent_id": agent_id,
        "bench_version": bench_version,
        "validator_hotkey": validator_hotkey,
        "lease_issued_at": _aware(lease_issued_at),
        "outcome": outcome,
        "failure_reason": failure_reason,
        # Already bounded by the hand-back model; kept whole, like the ticket.
        "failure_detail": failure_detail or None,
        "peak_bytes": telemetry.peak_bytes,
        "p50_bytes": telemetry.p50_bytes,
        "p95_bytes": telemetry.p95_bytes,
        "samples": telemetry.samples,
        "sampled_seconds": telemetry.sampled_seconds,
        "limit_bytes": telemetry.limit_bytes,
    }
    statement = insert(HarnessMemoryObservation).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="harness_memory_observations_lease_key",
        set_={key: statement.excluded[key] for key in values}
        | {"recorded_at": func.now()},
    )
    try:
        async with session.begin_nested():
            await session.execute(statement)
    except Exception as exc:
        if getattr(exc, "connection_invalidated", False):
            # A dead connection fails the surrounding report anyway; surface
            # the real cause instead of a later, unrelated error.
            raise
        logger.exception(
            "harness memory observation not recorded agent=%s validator=%s",
            agent_id,
            validator_hotkey,
        )


@dataclass(frozen=True)
class HarnessMemoryRollup:
    """One agent version's memory across every observed run."""

    agent_id: UUID
    runs: int
    scored_runs: int
    failed_runs: int
    oom_runs: int
    peak_bytes_max: int
    p50_bytes_median: int
    p95_bytes_median: int
    p95_bytes_max: int
    limit_bytes_max: int | None
    last_recorded_at: datetime


def _rollup_columns() -> list:
    observation = HarnessMemoryObservation
    return [
        observation.agent_id,
        func.count().label("runs"),
        func.count().filter(observation.outcome == "scored").label("scored_runs"),
        func.count().filter(observation.outcome == "failed").label("failed_runs"),
        func.count()
        .filter(observation.failure_detail.in_(_OOM_DETAILS))
        .label("oom_runs"),
        func.max(observation.peak_bytes).label("peak_bytes_max"),
        func.percentile_cont(0.5)
        .within_group(cast(observation.p50_bytes, Float))
        .label("p50_bytes_median"),
        func.percentile_cont(0.5)
        .within_group(cast(observation.p95_bytes, Float))
        .label("p95_bytes_median"),
        func.max(observation.p95_bytes).label("p95_bytes_max"),
        func.max(observation.limit_bytes).label("limit_bytes_max"),
        func.max(observation.recorded_at).label("last_recorded_at"),
    ]


def _rollup(row) -> HarnessMemoryRollup:
    return HarnessMemoryRollup(
        agent_id=row.agent_id,
        runs=int(row.runs),
        scored_runs=int(row.scored_runs),
        failed_runs=int(row.failed_runs),
        oom_runs=int(row.oom_runs),
        peak_bytes_max=int(row.peak_bytes_max),
        p50_bytes_median=round(row.p50_bytes_median),
        p95_bytes_median=round(row.p95_bytes_median),
        p95_bytes_max=int(row.p95_bytes_max),
        limit_bytes_max=(
            int(row.limit_bytes_max) if row.limit_bytes_max is not None else None
        ),
        last_recorded_at=_aware(row.last_recorded_at),
    )


async def agent_harness_memory(
    session: AsyncSession, *, agent_id: UUID, limit: int
) -> tuple[HarnessMemoryRollup | None, list[HarnessMemoryObservation]]:
    """One agent's rollup and its ``limit`` most recently leased observations."""
    rollup_row = (
        await session.execute(
            select(*_rollup_columns())
            .where(HarnessMemoryObservation.agent_id == agent_id)
            .group_by(HarnessMemoryObservation.agent_id)
        )
    ).one_or_none()
    runs = list(
        (
            await session.scalars(
                select(HarnessMemoryObservation)
                .where(HarnessMemoryObservation.agent_id == agent_id)
                # Lease order, not write order: a replayed report refreshes
                # recorded_at and must not push newer runs off the page.
                .order_by(
                    HarnessMemoryObservation.lease_issued_at.desc(),
                    HarnessMemoryObservation.id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )
    return (_rollup(rollup_row) if rollup_row is not None else None), runs


async def list_harness_memory(
    session: AsyncSession, *, limit: int, offset: int = 0
) -> list[HarnessMemoryRollup]:
    """Agents with observations, heaviest P95 first (ties by agent id)."""
    rows = (
        await session.execute(
            select(*_rollup_columns())
            .group_by(HarnessMemoryObservation.agent_id)
            .order_by(
                func.max(HarnessMemoryObservation.p95_bytes).desc(),
                HarnessMemoryObservation.agent_id,
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [_rollup(row) for row in rows]


__all__ = [
    "HarnessMemoryRollup",
    "agent_harness_memory",
    "list_harness_memory",
    "record_harness_memory",
]
