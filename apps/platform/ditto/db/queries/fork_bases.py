"""Storage and the ledger-side resolver for the fork-must-beat-base gate."""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.distinctive_content import ITEMS_VERSION
from ditto.api_server.fork_gate import (
    COURT_FORK_BASES,
    FORK_DETECTOR_VERSION,
    ForkBase,
    ForkDuel,
    fork_duel,
)
from ditto.api_server.koth import KothEntry
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    AgentForkBase,
    AthCopyCourtRecommendation,
)
from ditto.db.queries.confirmation_scores import confirmation_composites_by_seed


async def pending_fork_candidates(session: AsyncSession, *, limit: int) -> list[Agent]:
    """Indexed agents with no fork-base row whose whole history is indexed.

    Newest first, like :func:`ditto.db.queries.unreleased_copy.pending_candidates`:
    a fresh upload is classified within one sentinel tick, long before its
    scoring quorum can make it payable.
    """
    frontier = await session.scalar(
        select(func.min(Agent.created_at))
        .outerjoin(
            AgentDistinctiveItems,
            and_(
                AgentDistinctiveItems.agent_id == Agent.agent_id,
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
            ),
        )
        .where(AgentDistinctiveItems.agent_id.is_(None))
    )
    statement = (
        select(Agent)
        .join(AgentDistinctiveItems, AgentDistinctiveItems.agent_id == Agent.agent_id)
        .outerjoin(
            AgentForkBase,
            and_(
                AgentForkBase.agent_id == Agent.agent_id,
                AgentForkBase.detector_version == FORK_DETECTOR_VERSION,
            ),
        )
        .where(
            AgentDistinctiveItems.items_version == ITEMS_VERSION,
            AgentDistinctiveItems.error.is_(None),
            AgentForkBase.agent_id.is_(None),
        )
        .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
        .limit(limit)
    )
    if frontier is not None:
        statement = statement.where(Agent.created_at <= frontier)
    return list((await session.scalars(statement)).all())


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


async def court_fork_bases(session: AsyncSession, agent_id: UUID) -> set[UUID]:
    """Other-owner released bases the copy court cited for this artifact."""
    rows = await session.scalars(
        select(AthCopyCourtRecommendation.evidence).where(
            AthCopyCourtRecommendation.agent_id == agent_id
        )
    )
    bases: set[UUID] = set()
    for evidence in rows:
        if not isinstance(evidence, dict) or evidence.get("same_owner"):
            continue
        basis = evidence.get("basis")
        if basis not in COURT_FORK_BASES:
            continue
        matched = evidence.get("matched")
        if (
            basis == "matched_agent_public"
            and isinstance(matched, dict)
            and (matched_id := _uuid(matched.get("agent_id"))) is not None
        ):
            bases.add(matched_id)
        for base in evidence.get("bases_compared") or []:
            if isinstance(base, dict) and (base_id := _uuid(base.get("agent_id"))):
                bases.add(base_id)
    bases.discard(agent_id)
    return bases


async def store_fork_base(
    session: AsyncSession, *, agent_id: UUID, found: ForkBase | None
) -> None:
    await session.execute(
        insert(AgentForkBase)
        .values(
            agent_id=agent_id,
            detector_version=FORK_DETECTOR_VERSION,
            base_agent_id=found.base_agent_id if found is not None else None,
            score=found.score if found is not None else 0.0,
            shared_count=found.shared_count if found is not None else 0,
            basis=found.basis if found is not None else None,
            computed_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing()
    )


async def fork_bases_for(
    session: AsyncSession, agent_ids: Iterable[UUID]
) -> dict[UUID, AgentForkBase]:
    """Detected forks among ``agent_ids`` (rows naming a base only)."""
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.scalars(
        select(AgentForkBase).where(
            AgentForkBase.agent_id.in_(ids),
            AgentForkBase.detector_version == FORK_DETECTOR_VERSION,
            AgentForkBase.base_agent_id.is_not(None),
        )
    )
    return {row.agent_id: row for row in rows}


def _duel_entry(
    agent_id: UUID, hotkey: str, seeds: dict[int, float], bench_version: int
) -> KothEntry:
    ordered = sorted(seeds.items())
    return KothEntry(
        miner_hotkey=hotkey,
        agent_id=agent_id,
        composite=statistics.median(seeds.values()) if seeds else 0.0,
        first_seen=datetime.min.replace(tzinfo=UTC),
        raw_rank=1,
        bench_version=bench_version,
        confirmation_seeds=tuple(seed for seed, _ in ordered) if ordered else None,
        confirmation_composites=(
            tuple(value for _, value in ordered) if ordered else None
        ),
    )


async def resolve_fork_duels(
    session: AsyncSession,
    rows: Sequence[tuple[UUID, str]],
    *,
    bench_version: int,
    min_shared_seeds: int,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
) -> dict[UUID, ForkDuel]:
    """``agent_id -> ForkDuel`` for every detected fork among ``rows``.

    ``rows`` are ``(agent_id, miner_hotkey)``. Both sides are read from every
    confirmation score at ``bench_version``, whichever seed family produced
    it, so the duel uses exactly the seeds the two artifacts share.
    """
    bases = await fork_bases_for(session, (agent_id for agent_id, _ in rows))
    if not bases:
        return {}
    base_ids = {row.base_agent_id for row in bases.values() if row.base_agent_id}
    seeds = await confirmation_composites_by_seed(
        session, agent_ids=[*bases, *base_ids], bench_version=bench_version
    )
    hotkeys = dict(rows)
    base_hotkeys = dict(
        (
            await session.execute(
                select(Agent.agent_id, Agent.miner_hotkey).where(
                    Agent.agent_id.in_(base_ids)
                )
            )
        ).tuples()
    )
    duels: dict[UUID, ForkDuel] = {}
    for agent_id, row in bases.items():
        base_id = row.base_agent_id
        assert base_id is not None
        base_seeds = seeds.get(base_id, {})
        duels[agent_id] = fork_duel(
            _duel_entry(
                agent_id, hotkeys[agent_id], seeds.get(agent_id, {}), bench_version
            ),
            (
                _duel_entry(
                    base_id, base_hotkeys.get(base_id, ""), base_seeds, bench_version
                )
                if base_seeds
                else None
            ),
            base_agent_id=base_id,
            min_shared_seeds=min_shared_seeds,
            ceiling_band_clamp=ceiling_band_clamp,
            statistical_band_cap=statistical_band_cap,
        )
    return duels


__all__ = [
    "court_fork_bases",
    "fork_bases_for",
    "pending_fork_candidates",
    "resolve_fork_duels",
    "store_fork_base",
]
