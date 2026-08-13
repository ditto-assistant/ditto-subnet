"""Reads + insert-once writes for relative token-efficiency adjustments.

Both tables are append-only by contract (see the model docstrings):
``efficiency_cohort_snapshots`` gains one immutable row per epoch and
``efficiency_bonuses`` one immutable row per
``(agent_id, bench_version, epoch_index)``. Nothing in this module UPDATEs
either table — published adjustments never move.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import or_, select

from ditto.db.models import EfficiencyBonus, EfficiencyCohortSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.efficiency import CohortReference


async def get_snapshot(
    session: AsyncSession,
    *,
    bench_version: int,
    run_size: str,
    epoch_index: int,
) -> EfficiencyCohortSnapshot | None:
    """The frozen snapshot for one exact ``(bench_version, run_size, epoch)``."""
    return await session.scalar(
        select(EfficiencyCohortSnapshot).where(
            EfficiencyCohortSnapshot.bench_version == bench_version,
            EfficiencyCohortSnapshot.run_size == run_size,
            EfficiencyCohortSnapshot.epoch_index == epoch_index,
        )
    )


async def get_snapshot_by_id(
    session: AsyncSession, snapshot_id: UUID
) -> EfficiencyCohortSnapshot | None:
    """One frozen snapshot by its id (the audit / provenance read)."""
    return await session.get(EfficiencyCohortSnapshot, snapshot_id)


async def latest_snapshot(
    session: AsyncSession,
    *,
    bench_version: int,
    run_size: str,
    max_epoch_index: int,
    active_only: bool = False,
) -> EfficiencyCohortSnapshot | None:
    """The newest frozen snapshot at or before ``max_epoch_index``.

    ``active_only=True`` restricts to activated cohorts — the read used to
    derive the next epoch's quality floors from the previous *active* cohort.
    """
    statement = (
        select(EfficiencyCohortSnapshot)
        .where(
            EfficiencyCohortSnapshot.bench_version == bench_version,
            EfficiencyCohortSnapshot.run_size == run_size,
            EfficiencyCohortSnapshot.epoch_index <= max_epoch_index,
        )
        .order_by(EfficiencyCohortSnapshot.epoch_index.desc())
        .limit(1)
    )
    if active_only:
        statement = statement.where(EfficiencyCohortSnapshot.active.is_(True))
    return await session.scalar(statement)


async def insert_snapshot(
    session: AsyncSession, reference: CohortReference
) -> EfficiencyCohortSnapshot:
    """Persist one frozen cohort snapshot (caller-managed transaction).

    Flushes immediately so a concurrent materializer's duplicate epoch insert
    surfaces as ``IntegrityError`` here (the caller retries and re-reads the
    winner) rather than at commit.
    """
    snapshot = EfficiencyCohortSnapshot(
        snapshot_id=uuid4(),
        bench_version=reference.bench_version,
        run_size=reference.run_size,
        epoch_index=reference.epoch_index,
        active=reference.active,
        cohort_limit=reference.cohort_limit,
        n_min=reference.n_min,
        bonus_cap=reference.bonus_cap,
        curve_version=reference.curve_version,
        deep_bonus_cap=reference.deep_bonus_cap,
        deep_frontier_ratio=reference.deep_frontier_ratio,
        factor_alpha=reference.factor_alpha,
        minimum_factor=reference.minimum_factor,
        maximum_factor=reference.maximum_factor,
        quality_floor=reference.quality_floor,
        memory_floor=reference.memory_floor,
        reference_p25_tokens=reference.reference_p25_tokens,
        reference_median_tokens=reference.reference_median_tokens,
        members=[
            {
                "agent_id": str(member.agent_id),
                "miner_hotkey": member.miner_hotkey,
                "lineage_key": member.lineage_key,
                "composite": member.composite,
                "memory_mean": member.memory_mean,
                "token_total": member.token_total,
                "collapsed_agent_ids": [
                    str(agent_id) for agent_id in member.collapsed_agent_ids
                ],
                "first_seen": (
                    member.first_seen.isoformat()
                    if member.first_seen is not None
                    else None
                ),
            }
            for member in reference.members
        ],
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def get_bonus_rows(
    session: AsyncSession,
    agent_ids: Sequence[UUID],
    *,
    bench_versions: Mapping[UUID, int],
    epoch_index: int | None = None,
) -> dict[UUID, EfficiencyBonus]:
    """The frozen bonus row per requested agent at its authoritative version.

    ``epoch_index`` names WHICH epoch's row to read, explicitly rather than by
    accident of query ordering. Every consumer that feeds a score -- the
    validator ledger fold and the public board -- passes the CURRENT epoch, so
    an agent's bonus reflects its current efficiency rather than whatever epoch
    it was first measured in.

    ``None`` means "the newest row at or below any epoch", which is only for
    provenance/history readers that genuinely want the last thing assigned. It
    must not be used on a scoring path: a board that silently reads a stale
    epoch is exactly the freeze this key was widened to fix.
    """
    if not agent_ids:
        return {}
    statement = (
        select(EfficiencyBonus)
        .join(
            EfficiencyCohortSnapshot,
            EfficiencyCohortSnapshot.snapshot_id == EfficiencyBonus.snapshot_id,
        )
        .where(
            EfficiencyBonus.agent_id.in_(agent_ids),
            EfficiencyBonus.bench_version.in_(set(bench_versions.values())),
            # SQL CHECK constraints cannot inspect the referenced snapshot.
            # Fail a malformed/imported factor row closed unless its immutable
            # provenance really is curve v3; legacy null-factor rows remain
            # readable exactly as before.
            or_(
                EfficiencyBonus.factor.is_(None),
                EfficiencyCohortSnapshot.curve_version == 3,
            ),
        )
    )
    if epoch_index is not None:
        statement = statement.where(EfficiencyBonus.epoch_index == epoch_index)
    result = await session.scalars(statement.order_by(EfficiencyBonus.epoch_index))
    # Ascending epoch, so the last write per agent is the newest row. With an
    # explicit epoch there is at most one row per agent and the order is inert.
    return {
        row.agent_id: row
        for row in result
        if bench_versions.get(row.agent_id) == row.bench_version
    }


async def insert_bonus(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    epoch_index: int,
    snapshot_id: UUID,
    token_total: float | None,
    bonus: float,
    factor: float | None = None,
) -> EfficiencyBonus:
    """Persist one immutable bonus assignment (caller-managed transaction).

    Flushes immediately so a duplicate ``(agent_id, bench_version, epoch_index)``
    insert surfaces as ``IntegrityError`` to the caller's retry path — the
    earlier frozen row always wins; this function never overwrites.

    Immutability is now per EPOCH rather than per bench version. A later epoch
    inserts a new row beside this one; this row is never touched, so a published
    snapshot's numbers stay reproducible forever.
    """
    row = EfficiencyBonus(
        agent_id=agent_id,
        bench_version=bench_version,
        epoch_index=epoch_index,
        snapshot_id=snapshot_id,
        token_total=token_total,
        bonus=bonus,
        factor=factor,
    )
    session.add(row)
    await session.flush()
    return row


async def promote_v3_compatibility_placeholder(
    session: AsyncSession,
    row: EfficiencyBonus,
    *,
    token_total: float,
    factor: float,
) -> EfficiencyBonus:
    """Promote one neutral row emitted by the previous binary to v3 authority.

    The migration's trigger converts an old writer's would-be legacy award
    against a curve-v3 snapshot into ``bonus=0, factor=NULL``. Such a row is a
    non-authoritative compatibility placeholder, not a published assignment;
    after the new writer validates the exact signed v9 evidence it fills the
    token total and factor once. Every already-authoritative row remains
    immutable.
    """
    if row.factor is not None or row.bonus != 0.0:
        raise ValueError("only a neutral curve-v3 compatibility row may be promoted")
    row.token_total = token_total
    row.factor = factor
    await session.flush()
    return row
