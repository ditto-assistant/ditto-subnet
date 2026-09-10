"""Reads + insert-once writes for epoch-pinned validator ledger snapshots.

``ledger_epoch_snapshots`` is append-only by contract (see the model
docstring): one immutable row per ``(netuid, subnet_epoch_index)``. A pin is
what every validator folds for that chain epoch, so nothing in this module
UPDATEs a row -- a served pin never moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select

from ditto.db.models import LedgerEpochSnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class LedgerPinDraft:
    """Everything one pin stores, assembled before the insert-once write."""

    netuid: int
    epoch_index: int
    last_epoch_block: int
    pinned_block: int
    pinned_block_hash: str
    pinned_at: datetime
    bench_version: int
    entries: list[dict]
    context: dict
    ledger_digest: str
    champion_agent_id: UUID | None = None
    champion_owner_root: str | None = None
    incumbent_agent_id: UUID | None = None


async def get_pin(
    session: AsyncSession, *, netuid: int, epoch_index: int
) -> LedgerEpochSnapshot | None:
    """The pinned ledger for one exact chain epoch, if it was ever taken."""
    return await session.scalar(
        select(LedgerEpochSnapshot).where(
            LedgerEpochSnapshot.netuid == netuid,
            LedgerEpochSnapshot.epoch_index == epoch_index,
        )
    )


async def latest_pin(
    session: AsyncSession,
    *,
    netuid: int,
    max_epoch_index: int | None = None,
) -> LedgerEpochSnapshot | None:
    """The newest pin at or before ``max_epoch_index`` (any epoch when ``None``)."""
    statement = (
        select(LedgerEpochSnapshot)
        .where(LedgerEpochSnapshot.netuid == netuid)
        .order_by(LedgerEpochSnapshot.epoch_index.desc())
        .limit(1)
    )
    if max_epoch_index is not None:
        statement = statement.where(LedgerEpochSnapshot.epoch_index <= max_epoch_index)
    return await session.scalar(statement)


async def list_pins(
    session: AsyncSession, *, netuid: int, limit: int
) -> Sequence[LedgerEpochSnapshot]:
    """Newest pins first, bounded; the per-epoch crown history read."""
    result = await session.scalars(
        select(LedgerEpochSnapshot)
        .where(LedgerEpochSnapshot.netuid == netuid)
        .order_by(LedgerEpochSnapshot.epoch_index.desc())
        .limit(max(1, limit))
    )
    return result.all()


async def insert_pin(
    session: AsyncSession, draft: LedgerPinDraft
) -> LedgerEpochSnapshot:
    """Persist one pin (caller-managed transaction).

    Flushes immediately so a concurrent materializer's duplicate-epoch insert
    surfaces as ``IntegrityError`` here -- the caller then rolls back and
    re-reads the winner -- rather than at commit.
    """
    row = LedgerEpochSnapshot(
        snapshot_id=uuid4(),
        netuid=draft.netuid,
        epoch_index=draft.epoch_index,
        last_epoch_block=draft.last_epoch_block,
        pinned_block=draft.pinned_block,
        pinned_block_hash=draft.pinned_block_hash,
        pinned_at=draft.pinned_at,
        bench_version=draft.bench_version,
        entries=draft.entries,
        context=draft.context,
        champion_agent_id=draft.champion_agent_id,
        champion_owner_root=draft.champion_owner_root,
        incumbent_agent_id=draft.incumbent_agent_id,
        ledger_digest=draft.ledger_digest,
    )
    session.add(row)
    await session.flush()
    return row
