"""Ownership-gated builder for a miner's bench v13+ per-case gate notes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.gate_evidence import MinerGateNotesResponse
from ditto.api_server.gate_evidence import owner_gate_notes
from ditto.db.models import Agent, Score


async def load_owned_gate_notes(
    session: AsyncSession,
    *,
    hotkey: str,
    agent_id: UUID,
) -> MinerGateNotesResponse | None:
    """Every accepted run's gate notes for one of ``hotkey``'s own agents.

    ``None`` when the agent does not exist or belongs to another hotkey -- the
    caller maps both to the same 404, so the endpoint never confirms that a
    foreign agent id exists. Runs without stored evidence (pre-v13, or a scorer
    that emitted none) are simply absent from ``runs``.
    """
    agent = await session.scalar(
        select(Agent).where(
            Agent.agent_id == agent_id,
            Agent.miner_hotkey == hotkey,
        )
    )
    if agent is None:
        return None
    scores = list(
        (await session.scalars(select(Score).where(Score.agent_id == agent_id))).all()
    )
    return owner_gate_notes(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        agent_status=str(agent.status),
        scores=scores,
    )
