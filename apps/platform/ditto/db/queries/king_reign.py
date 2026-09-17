"""King-only source release anchored to completed winner emission evidence.

Revealed weights express validator intent, not a completed payout. Legacy
weight observations are retained separately and never start the release clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentKingship


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps to the Postgres UTC contract."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


SOURCE_RELEASE_GATE_VERSION = "completed-winner-emission-v1"


@dataclass(frozen=True)
class KingReveal:
    """Release state; only completed emission confirmation starts the embargo."""

    first_crowned_at: datetime
    weight_confirmed_at: datetime | None
    emission_confirmed_at: datetime | None = None


@dataclass(frozen=True)
class KingEmissionProof:
    """Verified payout-block evidence, attributed to one immutable ledger pin.

    ``confirmed_at`` is the finalized payout block time, not observation time.
    The caller must verify actual winner earnings for the exact submission;
    merely observing a positive weight or an intended allocation is insufficient.
    """

    confirmed_at: datetime
    block: int
    block_hash: str
    epoch_index: int
    ledger_digest: str
    evidence: dict = field(default_factory=dict)


async def record_emission_confirmed(
    session: AsyncSession, *, agent_id: UUID, proof: KingEmissionProof
) -> None:
    """Atomically persist the first verified emission proof for an ever-king."""
    if (
        proof.confirmed_at.tzinfo is None
        or proof.block < 1
        or proof.epoch_index < 0
        or not proof.block_hash
        or not proof.ledger_digest
    ):
        raise ValueError("emission proof requires a dated finalized block and ledger")
    await session.execute(
        update(AgentKingship)
        .where(
            AgentKingship.agent_id == agent_id,
            AgentKingship.emission_confirmed_at.is_(None),
            AgentKingship.first_crowned_at <= proof.confirmed_at,
        )
        .values(
            emission_confirmed_at=proof.confirmed_at,
            emission_block=proof.block,
            emission_block_hash=proof.block_hash,
            emission_epoch_index=proof.epoch_index,
            emission_ledger_digest=proof.ledger_digest,
            emission_evidence=proof.evidence,
        )
    )


async def list_emission_unconfirmed_kings(
    session: AsyncSession,
) -> list[tuple[UUID, str]]:
    """Ever-kings still lacking exact-submission completed emission proof."""
    rows = (
        await session.execute(
            select(AgentKingship.agent_id, Agent.miner_hotkey)
            .join(Agent, Agent.agent_id == AgentKingship.agent_id)
            .where(AgentKingship.emission_confirmed_at.is_(None))
        )
    ).all()
    return [(agent_id, hotkey) for agent_id, hotkey in rows]


async def record_first_crowned(
    session: AsyncSession, *, agent_id: UUID, now: datetime
) -> None:
    """Record an agent's first coronation. Idempotent and write-once.

    A later re-coronation must NOT move ``first_crowned_at``: it is the
    eligibility marker, not the release clock. Callers run this in a
    best-effort, isolated transaction, so a duplicate race (two validators
    crowning the same champion at once) is a harmless no-op.
    """
    if await session.get(AgentKingship, agent_id) is not None:
        return
    session.add(AgentKingship(agent_id=agent_id, first_crowned_at=now))


async def record_weight_confirmed(
    session: AsyncSession, *, agent_id: UUID, now: datetime
) -> None:
    """Stamp the first on-chain weight confirmation for an ever-king agent.

    Write-once and safe to call repeatedly: a no-op unless the agent is a
    known king with no weight observation yet. This does not permit disclosure.
    """
    row = await session.get(AgentKingship, agent_id)
    if row is None or row.weight_confirmed_at is not None:
        return
    row.weight_confirmed_at = now


async def list_unconfirmed_kings(session: AsyncSession) -> list[tuple[UUID, str]]:
    """Return ``(agent_id, miner_hotkey)`` for ever-kings not yet weight-confirmed.

    The caller matches each hotkey against the revealed weight matrix to decide
    whether to confirm. Usually a very small set (kings are rare), so this stays
    cheap to sweep on the score path.
    """
    rows = (
        await session.execute(
            select(AgentKingship.agent_id, Agent.miner_hotkey)
            .join(Agent, Agent.agent_id == AgentKingship.agent_id)
            .where(AgentKingship.weight_confirmed_at.is_(None))
        )
    ).all()
    return [(agent_id, hotkey) for agent_id, hotkey in rows]


async def get_king_reveal(
    session: AsyncSession,
    *,
    agent_ids: list[UUID] | set[UUID] | tuple[UUID, ...],
) -> dict[UUID, KingReveal]:
    """Return the reveal state for agents that have held the crown."""
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AgentKingship.agent_id,
                AgentKingship.first_crowned_at,
                AgentKingship.weight_confirmed_at,
                AgentKingship.emission_confirmed_at,
            ).where(AgentKingship.agent_id.in_(agent_ids))
        )
    ).all()
    return {
        agent_id: KingReveal(
            first_crowned_at=_as_utc(first_crowned_at),
            weight_confirmed_at=(
                _as_utc(weight_confirmed_at)
                if weight_confirmed_at is not None
                else None
            ),
            emission_confirmed_at=(
                _as_utc(emission_confirmed_at)
                if emission_confirmed_at is not None
                else None
            ),
        )
        for (
            agent_id,
            first_crowned_at,
            weight_confirmed_at,
            emission_confirmed_at,
        ) in rows
    }
