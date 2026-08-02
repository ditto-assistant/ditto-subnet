"""Unit tests for the two-stage king-reign ledger.

Exercises the real ORM + SQLite-in-memory engine so the write-once invariants
(a later re-coronation never moves ``first_crowned_at``; a re-confirmation never
moves ``weight_confirmed_at``) are enforced by the same code path production
runs, not a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AgentStatus
from ditto.db.queries.king_reign import (
    get_king_reveal,
    list_unconfirmed_kings,
    record_first_crowned,
    record_weight_confirmed,
)

pytestmark = pytest.mark.asyncio


async def _agent(session: AsyncSession, agent_id: object, *, hotkey: str) -> None:
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name="alpha-agent",
            sha256="deadbeef" * 8,
            size_bytes=524288,
            status=AgentStatus.SCORED,
        )
    )
    await session.flush()


async def test_first_coronation_is_write_once(session: AsyncSession) -> None:
    agent_id = uuid4()
    await _agent(session, agent_id, hotkey="5King")
    first = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    later = first + timedelta(hours=5)

    await record_first_crowned(session, agent_id=agent_id, now=first)
    # A later re-coronation must not move the eligibility marker.
    await record_first_crowned(session, agent_id=agent_id, now=later)

    reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]
    assert reveal.first_crowned_at == first
    # Not on-chain confirmed yet: window has not started.
    assert reveal.weight_confirmed_at is None


async def test_weight_confirmation_is_write_once_and_king_only(
    session: AsyncSession,
) -> None:
    king_id = uuid4()
    commoner_id = uuid4()
    await _agent(session, king_id, hotkey="5King")
    await _agent(session, commoner_id, hotkey="5Commoner")
    crowned = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    confirmed = crowned + timedelta(hours=3)
    later = confirmed + timedelta(hours=9)

    await record_first_crowned(session, agent_id=king_id, now=crowned)
    # Confirming a non-king is a no-op (no kingship row to anchor).
    await record_weight_confirmed(session, agent_id=commoner_id, now=confirmed)
    await record_weight_confirmed(session, agent_id=king_id, now=confirmed)
    # A later re-confirmation must not move the anchored window.
    await record_weight_confirmed(session, agent_id=king_id, now=later)

    reveals = await get_king_reveal(session, agent_ids=[king_id, commoner_id])
    assert set(reveals) == {king_id}
    assert reveals[king_id].first_crowned_at == crowned
    assert reveals[king_id].weight_confirmed_at == confirmed


async def test_list_unconfirmed_kings_returns_only_pending_with_hotkey(
    session: AsyncSession,
) -> None:
    pending_id = uuid4()
    confirmed_id = uuid4()
    await _agent(session, pending_id, hotkey="5Pending")
    await _agent(session, confirmed_id, hotkey="5Confirmed")
    now = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)

    await record_first_crowned(session, agent_id=pending_id, now=now)
    await record_first_crowned(session, agent_id=confirmed_id, now=now)
    await record_weight_confirmed(session, agent_id=confirmed_id, now=now)

    pending = await list_unconfirmed_kings(session)
    assert pending == [(pending_id, "5Pending")]
    assert await get_king_reveal(session, agent_ids=[]) == {}
