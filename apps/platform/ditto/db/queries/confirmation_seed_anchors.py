"""Reads + pin-once writes for the finalized-block confirmation seed anchors.

One :class:`~ditto.db.models.ConfirmationSeedAnchor` row per
``(champion, bench_version)`` reign binds the champion-anchored CRN seed family
(:mod:`ditto.api_server.crn`) to a chain block nobody could know at
submission time. The row is created with a null hash at the reign's first
continual-retest claim, pinned exactly once when ``anchor_block`` is finalized,
and never re-pinned: the family it names is what validators already scored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ditto.db.models import ConfirmationSeedAnchor

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def get_confirmation_seed_anchor(
    session: AsyncSession, *, champion_agent_id: UUID, bench_version: int
) -> ConfirmationSeedAnchor | None:
    """The reign's anchor row, pinned or still waiting, or None when never asked."""
    return await session.get(ConfirmationSeedAnchor, (champion_agent_id, bench_version))


async def list_pinned_confirmation_seed_anchors(
    session: AsyncSession, *, bench_version: int
) -> list[ConfirmationSeedAnchor]:
    """Every pinned reign anchor for one version, oldest anchor block first.

    Deterministic order matters: the ledger serves this list to the whole fleet
    and a validator binds a historical seed to the first anchor that derives it.
    """
    rows = await session.scalars(
        select(ConfirmationSeedAnchor)
        .where(
            ConfirmationSeedAnchor.bench_version == bench_version,
            ConfirmationSeedAnchor.anchor_block_hash.is_not(None),
        )
        .order_by(
            ConfirmationSeedAnchor.anchor_block,
            ConfirmationSeedAnchor.champion_agent_id,
        )
    )
    return list(rows.all())


async def list_confirmation_seed_anchors(
    session: AsyncSession, *, bench_version: int, limit: int | None = None
) -> list[ConfirmationSeedAnchor]:
    """Every reign anchor of one version, pinned or waiting, oldest anchor first.

    The operator read surface: "is the reign pinned yet?" is the first live
    question at a binding version, and a waiting row is exactly what the
    ledger does not serve.
    """
    query = (
        select(ConfirmationSeedAnchor)
        .where(ConfirmationSeedAnchor.bench_version == bench_version)
        .order_by(
            ConfirmationSeedAnchor.anchor_block,
            ConfirmationSeedAnchor.champion_agent_id,
        )
    )
    if limit is not None:
        query = query.limit(limit)
    return list((await session.scalars(query)).all())


async def list_unpinned_confirmation_seed_anchors(
    session: AsyncSession, *, up_to_block: int, limit: int
) -> list[ConfirmationSeedAnchor]:
    """Waiting anchors whose height the chain head has already reached.

    Bounded and oldest first: these are the rows one claim may try to pin,
    and the chain read for each happens outside any write transaction, so the
    per-claim RPC budget is ``limit`` reads at most.
    """
    rows = await session.scalars(
        select(ConfirmationSeedAnchor)
        .where(
            ConfirmationSeedAnchor.anchor_block_hash.is_(None),
            ConfirmationSeedAnchor.anchor_block <= int(up_to_block),
        )
        .order_by(
            ConfirmationSeedAnchor.anchor_block,
            ConfirmationSeedAnchor.champion_agent_id,
        )
        .limit(max(0, int(limit)))
    )
    return list(rows.all())


async def ensure_confirmation_seed_anchor(
    session: AsyncSession,
    *,
    champion_agent_id: UUID,
    bench_version: int,
    ready_block: int,
    delta: int,
) -> ConfirmationSeedAnchor:
    """Return the reign's anchor row, creating it (unpinned) on first sight.

    ``INSERT ... ON CONFLICT DO NOTHING`` on the reign key: two validators
    claiming the same fresh reign in parallel converge on one ``ready_block``
    -- whichever committed first -- instead of racing to two different anchor
    heights. The caller owns the transaction.
    """
    values = {
        "champion_agent_id": champion_agent_id,
        "bench_version": int(bench_version),
        "ready_block": int(ready_block),
        "anchor_block": int(ready_block) + int(delta),
    }
    insert = (
        pg_insert(ConfirmationSeedAnchor)
        if session.get_bind().dialect.name == "postgresql"
        else sqlite_insert(ConfirmationSeedAnchor)
    )
    await session.execute(
        insert.values(**values).on_conflict_do_nothing(
            index_elements=["champion_agent_id", "bench_version"]
        )
    )
    row = await session.get(
        ConfirmationSeedAnchor,
        (champion_agent_id, int(bench_version)),
        populate_existing=True,
    )
    assert row is not None
    return row


async def pin_confirmation_seed_anchor(
    session: AsyncSession,
    anchor: ConfirmationSeedAnchor,
    *,
    block_hash: str,
    now: datetime,
) -> ConfirmationSeedAnchor:
    """Pin the finalized hash exactly once; a second pin is a no-op.

    Idempotent rather than raising: two claims can observe finality in the
    same tick and both try to pin the same hash. The first write wins and the
    second re-reads it, so both hand out the same family.
    """
    if anchor.anchor_block_hash is not None:
        return anchor
    normalized = block_hash.strip().lower()
    if not normalized.startswith("0x"):
        normalized = "0x" + normalized
    anchor.anchor_block_hash = normalized
    anchor.pinned_at = now
    await session.flush()
    return anchor
