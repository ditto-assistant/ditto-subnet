"""When a benchmark-scoped queue removal counts, and when it has been reversed.

Four surfaces ask this table the same question -- validator admission
(:mod:`ditto.db.queries.benchmark_admission`), retry triage
(:mod:`ditto.db.queries.retry_state`), retirement
(:mod:`ditto.db.queries.retirement`) and the operator detail route -- and before
reinstatement existed the answer was simply "a row exists". It is now "a row
exists *and has not been reinstated*", and that qualifier has to live in one
place: a reader that forgets it would keep a reinstated submission out of the
validator queue while the operator surface reports it back in, which is a silent
version of exactly the one-way behaviour reinstatement was built to remove.

A reversal never deletes the removal row. Deleting it would orphan the
``validator_lease_audit`` rows the eviction wrote (``GET
/admin/lease-revocations?action=operator_evicted`` reads them) and erase the
evictor's recorded justification. ``reinstated_at`` resolves the row instead, so
the era keeps its whole removal history and every predicate here stays a single
indexed column read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ColumnElement, select

from ditto.db.models import ValidatorQueueReinstatement, ValidatorQueueWithdrawal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def removal_in_force() -> ColumnElement[bool]:
    """SQL for "this removal row still keeps the submission out of the queue"."""
    return ValidatorQueueWithdrawal.reinstated_at.is_(None)


def is_in_force(removal: ValidatorQueueWithdrawal | None) -> bool:
    """The same verdict for a row already loaded in Python."""
    return removal is not None and removal.reinstated_at is None


def is_eviction(removal: ValidatorQueueWithdrawal) -> bool:
    """Whether this removal took live leases, rather than tidying up a corpse.

    Reads the tri-state ``evicted_validator_hotkeys``: ``NULL`` is an ordinary
    withdrawal, and any list -- including the empty one -- means the eviction
    route ran.
    """
    return removal.evicted_validator_hotkeys is not None


async def load_queue_removal(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    for_update: bool = False,
    in_force_only: bool = True,
) -> ValidatorQueueWithdrawal | None:
    """The submission's removal from one benchmark era, newest first.

    ``in_force_only`` (the default) answers the queue question: a reinstated row
    is not a removal any more. Pass ``False`` for the operator read path, which
    must still be able to show a reversed eviction — an eviction that happened
    and was undone is more informative than no eviction at all.
    """
    statement = select(ValidatorQueueWithdrawal).where(
        ValidatorQueueWithdrawal.agent_id == agent_id,
        ValidatorQueueWithdrawal.bench_version == bench_version,
    )
    if in_force_only:
        statement = statement.where(removal_in_force())
    statement = statement.order_by(
        ValidatorQueueWithdrawal.created_at.desc(),
        ValidatorQueueWithdrawal.withdrawal_id.desc(),
    ).limit(1)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def load_reinstatement(
    session: AsyncSession, *, withdrawal_id: UUID
) -> ValidatorQueueReinstatement | None:
    """The reversal of one removal, if it has been reversed."""
    return await session.scalar(
        select(ValidatorQueueReinstatement).where(
            ValidatorQueueReinstatement.withdrawal_id == withdrawal_id
        )
    )


__all__ = [
    "is_eviction",
    "is_in_force",
    "load_queue_removal",
    "load_reinstatement",
    "removal_in_force",
]
