"""Storage and context for the unreleased-copy signal.

The pure signal is :mod:`ditto.api_server.unreleased_copy`; this module owns
the index rows, the SQL-side overlap scan, owner and release resolution as of a
candidate's upload, and the recorded shadow signals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, and_, cast, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.distinctive_content import (
    ITEMS_VERSION,
    ArtifactItems,
    public_item_keys,
)
from ditto.api_server.unreleased_copy import (
    CopySignal,
    IndexedAgent,
    canonical_owners,
)
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    UnreleasedCopySignal,
)
from ditto.db.queries.artifact_release import list_public_source_releases
from ditto.db.queries.artifact_release_settings import artifact_release_policy_as_of
from ditto.db.queries.attestation import list_linked_hotkeys
from ditto.db.queries.payments import get_miner_coldkeys_for_agents
from ditto.db.queries.scores import SCORING_QUORUM


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def index_keys(extracted: ArtifactItems) -> list[int]:
    """Sorted item keys of an artifact with the public corpus removed."""
    public = public_item_keys()
    return sorted(k for k in extracted.items if k not in public)


async def store_items(
    session: AsyncSession,
    *,
    agent_id: UUID,
    extracted: ArtifactItems | None,
    error: str | None = None,
) -> None:
    """Insert or replace one agent's index row (``error`` when unreadable)."""
    keys = index_keys(extracted) if extracted is not None else []
    values = {
        "agent_id": agent_id,
        "items_version": ITEMS_VERSION,
        "item_keys": keys,
        "item_count": len(keys),
        "error": None if extracted is not None else (error or "unreadable"),
        "indexed_at": datetime.now(UTC),
    }
    statement = insert(AgentDistinctiveItems).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[AgentDistinctiveItems.agent_id],
            set_={k: statement.excluded[k] for k in values if k != "agent_id"},
        )
    )


async def agents_missing_items(session: AsyncSession, *, limit: int) -> list[Agent]:
    """Agents with no current-version index row, oldest first.

    Oldest first, because a candidate is only evaluated once every agent
    uploaded before it is indexed (:func:`pending_candidates`).
    """
    missing = (
        select(Agent)
        .outerjoin(
            AgentDistinctiveItems,
            and_(
                AgentDistinctiveItems.agent_id == Agent.agent_id,
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
            ),
        )
        .where(AgentDistinctiveItems.agent_id.is_(None))
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(limit)
    )
    return list((await session.scalars(missing)).all())


async def pending_candidates(
    session: AsyncSession, *, signal_version: str, limit: int
) -> list[Agent]:
    """Indexed agents without a signal whose whole upload history is indexed.

    Newest first, so a fresh upload is evaluated within one tick even while
    an old backlog is still being worked.
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
            UnreleasedCopySignal,
            and_(
                UnreleasedCopySignal.agent_id == Agent.agent_id,
                UnreleasedCopySignal.signal_version == signal_version,
            ),
        )
        .where(
            AgentDistinctiveItems.items_version == ITEMS_VERSION,
            AgentDistinctiveItems.error.is_(None),
            UnreleasedCopySignal.agent_id.is_(None),
        )
        .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
        .limit(limit)
    )
    if frontier is not None:
        statement = statement.where(Agent.created_at <= frontier)
    return list((await session.scalars(statement)).all())


@dataclass(frozen=True)
class SignalContext:
    candidate: IndexedAgent
    others: list[IndexedAgent]


async def load_context(session: AsyncSession, agent: Agent) -> SignalContext | None:
    """Everything :func:`ditto.api_server.unreleased_copy.evaluate` needs.

    Each other agent carries only its overlap with the candidate (computed in
    Postgres), which is all the signal reads. Owners include payment coldkeys
    and the candidate's attested hotkey links; release times are publication
    evidence under the policy in force when the candidate was uploaded.
    """
    row = await session.get(AgentDistinctiveItems, agent.agent_id)
    if row is None or row.error is not None or row.items_version != ITEMS_VERSION:
        return None
    keys = list(row.item_keys)
    uploaded_at = _utc(agent.created_at)
    if keys:
        overlap = (
            await session.execute(
                select(
                    AgentDistinctiveItems.agent_id,
                    func.array(
                        select(func.unnest(AgentDistinctiveItems.item_keys))
                        .intersect(select(func.unnest(cast(keys, ARRAY(BigInteger)))))
                        .scalar_subquery()
                    ),
                )
                .join(Agent, Agent.agent_id == AgentDistinctiveItems.agent_id)
                .where(
                    AgentDistinctiveItems.agent_id != agent.agent_id,
                    AgentDistinctiveItems.items_version == ITEMS_VERSION,
                    AgentDistinctiveItems.error.is_(None),
                    Agent.created_at <= agent.created_at,
                )
            )
        ).all()
    else:
        overlap = []
    shared = {aid: frozenset(ks) for aid, ks in overlap if ks}
    rows = (
        (
            await session.scalars(
                select(Agent).where(Agent.agent_id.in_([*shared, agent.agent_id]))
            )
        ).all()
        if shared
        else [agent]
    )
    by_id = {a.agent_id: a for a in rows}
    coldkeys = await get_miner_coldkeys_for_agents(session, agent_ids=set(by_id))
    from ditto.api_server.attestation import expected_netuid

    links = [
        (agent.miner_hotkey, link.hotkey)
        for link in await list_linked_hotkeys(
            session, hotkey=agent.miner_hotkey, netuid=expected_netuid()
        )
    ]
    owners = canonical_owners(
        ((a.agent_id, a.miner_hotkey, coldkeys.get(a.agent_id)) for a in rows), links
    )
    policy = await artifact_release_policy_as_of(session, at=uploaded_at)
    released = await list_public_source_releases(
        session, agent_ids=list(shared), quorum=SCORING_QUORUM, policy=policy
    )

    def indexed(a: Agent, item_keys: frozenset[int]) -> IndexedAgent:
        return IndexedAgent(
            agent_id=a.agent_id,
            owner=owners[a.agent_id],
            uploaded_at=_utc(a.created_at),
            released_at=released.get(a.agent_id),
            keys=item_keys,
            name=a.name,
            version=a.version,
            hotkey=a.miner_hotkey,
            status=a.status.value,
        )

    return SignalContext(
        candidate=indexed(agent, frozenset(keys)),
        others=[indexed(by_id[aid], ks) for aid, ks in shared.items() if aid in by_id],
    )


def signal_payload(signal: CopySignal, *, evidence_status: str) -> dict[str, Any]:
    """JSON evidence for one signal (operator-only)."""
    return {
        "signal_version": signal.version,
        "items_version": ITEMS_VERSION,
        "threshold": signal.threshold,
        "flagged": signal.flagged,
        "top_score": signal.top_score,
        "candidate_item_count": signal.candidate_item_count,
        "compared_agent_count": signal.compared_agent_count,
        "excluded": signal.excluded,
        "evidence_status": evidence_status,
        "matches": [
            {
                "agent_id": str(m.matched_agent_id),
                "agent_name": m.matched_name,
                "agent_version": m.matched_version,
                "miner_hotkey": m.matched_hotkey,
                "agent_status": m.matched_status,
                "uploaded_at": m.matched_uploaded_at.isoformat(),
                "released_at": (
                    m.matched_released_at.isoformat() if m.matched_released_at else None
                ),
                "release_state": (
                    "released_after_upload"
                    if m.matched_released_at is not None
                    else "unreleased"
                ),
                "score": m.score,
                "shared_count": m.shared_count,
                "shared_by_kind": m.shared_by_kind,
                "items": [
                    {
                        "kind": it.kind,
                        "text": it.preview,
                        "candidate": f"{it.candidate_path}:{it.candidate_line}",
                        "matched": f"{it.matched_path}:{it.matched_line}",
                    }
                    for it in m.items
                ],
            }
            for m in signal.matches
        ],
    }


async def record_signal(
    session: AsyncSession, *, signal: CopySignal, mode: str, payload: dict[str, Any]
) -> None:
    values = {
        "agent_id": signal.candidate_agent_id,
        "signal_version": signal.version,
        "mode": mode,
        "threshold": signal.threshold,
        "flagged": signal.flagged,
        "top_score": signal.top_score,
        "top_matched_agent_id": (
            signal.matches[0].matched_agent_id if signal.matches else None
        ),
        "compared_agent_count": signal.compared_agent_count,
        "candidate_item_count": signal.candidate_item_count,
        "evidence": payload,
        "computed_at": datetime.now(UTC),
    }
    statement = insert(UnreleasedCopySignal).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                UnreleasedCopySignal.agent_id,
                UnreleasedCopySignal.signal_version,
            ],
            set_={
                k: statement.excluded[k]
                for k in values
                if k not in {"agent_id", "signal_version"}
            },
        )
    )


async def list_signals(
    session: AsyncSession,
    *,
    signal_version: str,
    flagged_only: bool,
    min_score: float,
    limit: int,
    offset: int,
) -> tuple[int, Sequence[tuple[UnreleasedCopySignal, Agent]]]:
    where = [
        UnreleasedCopySignal.signal_version == signal_version,
        UnreleasedCopySignal.top_score >= min_score,
    ]
    if flagged_only:
        where.append(UnreleasedCopySignal.flagged.is_(True))
    total = await session.scalar(
        select(func.count()).select_from(UnreleasedCopySignal).where(*where)
    )
    rows = (
        await session.execute(
            select(UnreleasedCopySignal, Agent)
            .join(Agent, Agent.agent_id == UnreleasedCopySignal.agent_id)
            .where(*where)
            .order_by(UnreleasedCopySignal.top_score.desc(), Agent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).tuples()
    return int(total or 0), list(rows.all())


async def index_coverage(
    session: AsyncSession, *, signal_version: str
) -> dict[str, Any]:
    """Counts that say how far the backfill and evaluation have got."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM agents) AS agents,
                  (SELECT count(*) FROM agent_distinctive_items
                    WHERE items_version = :iv) AS indexed,
                  (SELECT count(*) FROM agent_distinctive_items
                    WHERE items_version = :iv AND error IS NOT NULL) AS unreadable,
                  (SELECT min(a.created_at) FROM agents a
                    LEFT JOIN agent_distinctive_items d
                      ON d.agent_id = a.agent_id AND d.items_version = :iv
                    WHERE d.agent_id IS NULL) AS oldest_unindexed_at,
                  (SELECT count(*) FROM unreleased_copy_signals
                    WHERE signal_version = :sv) AS evaluated,
                  (SELECT count(*) FROM unreleased_copy_signals
                    WHERE signal_version = :sv AND flagged) AS flagged
                """
            ),
            {"iv": ITEMS_VERSION, "sv": signal_version},
        )
    ).one()
    return dict(row._mapping)
