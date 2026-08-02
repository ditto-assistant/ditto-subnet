"""Unit tests for the on-chain king-weight confirmation sweep.

Covers the post-commit sweep that arms a king's public source-release window
only once validators' REVEALED on-chain weights (post commit-reveal) are seen
set on the miner. Uses a real ORM + SQLite engine and a stub chain client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_server.endpoints.validator import (
    _KING_WEIGHT_CHECK_INTERVAL,
    _confirm_king_onchain_weights,
)
from ditto.chain.models import ChainWeight, ChainWeightsSnapshot, ChainWeightVector
from ditto.db.models import Agent, AgentStatus
from ditto.db.queries.king_reign import get_king_reveal, record_first_crowned

pytestmark = pytest.mark.asyncio

_CROWNED_AT = datetime(2026, 7, 20, tzinfo=UTC)
_NOW = datetime(2026, 7, 22, tzinfo=UTC)


@pytest.fixture
def maker(
    session_maker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Local alias for the root Postgres ``session_maker``."""
    return session_maker


def _app_state(**overrides: object) -> SimpleNamespace:
    state = SimpleNamespace(config=SimpleNamespace(chain=SimpleNamespace(netuid=118)))
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _snapshot(*hotkeys: str) -> ChainWeightsSnapshot:
    return ChainWeightsSnapshot(
        netuid=118,
        block=1,
        block_hash="0x00",
        owner_hotkey=None,
        vectors=(
            ChainWeightVector(
                validator_uid=0,
                validator_hotkey="5Validator",
                weights=tuple(
                    ChainWeight(uid=index + 1, hotkey=hotkey, value=100)
                    for index, hotkey in enumerate(hotkeys)
                ),
            ),
        ),
    )


async def _crown_agent(maker: async_sessionmaker[AsyncSession], *, hotkey: str) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="agent",
                sha256="ab" * 32,
                size_bytes=1024,
                status=AgentStatus.SCORED,
            )
        )
        await session.flush()
        await record_first_crowned(session, agent_id=agent_id, now=_CROWNED_AT)
    return agent_id


async def test_confirms_when_king_has_revealed_onchain_weight(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _crown_agent(maker, hotkey="5King")
    chain: Any = SimpleNamespace(get_weights=AsyncMock(return_value=_snapshot("5King")))

    async with maker() as session:
        await _confirm_king_onchain_weights(_app_state(), chain, session, now=_NOW)
        reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]

    assert reveal.weight_confirmed_at == _NOW
    chain.get_weights.assert_awaited_once_with(118)


async def test_does_not_confirm_without_onchain_weight(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _crown_agent(maker, hotkey="5King")
    chain: Any = SimpleNamespace(
        get_weights=AsyncMock(return_value=_snapshot("5SomeoneElse"))
    )

    async with maker() as session:
        await _confirm_king_onchain_weights(_app_state(), chain, session, now=_NOW)
        reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]

    assert reveal.weight_confirmed_at is None


async def test_throttled_within_interval_skips_the_chain(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await _crown_agent(maker, hotkey="5King")
    chain: Any = SimpleNamespace(get_weights=AsyncMock(return_value=_snapshot("5King")))
    state = _app_state(king_weight_checked_at=_NOW)

    async with maker() as session:
        await _confirm_king_onchain_weights(
            state,
            chain,
            session,
            now=_NOW + _KING_WEIGHT_CHECK_INTERVAL - timedelta(seconds=1),
        )

    chain.get_weights.assert_not_awaited()


async def test_no_chain_read_when_no_king_is_pending(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # An already-confirmed king leaves nothing pending, so the chain is untouched.
    agent_id = await _crown_agent(maker, hotkey="5King")
    chain: Any = SimpleNamespace(get_weights=AsyncMock(return_value=_snapshot("5King")))
    async with maker() as session:
        await _confirm_king_onchain_weights(_app_state(), chain, session, now=_NOW)
    chain.get_weights.reset_mock()

    async with maker() as session:
        await _confirm_king_onchain_weights(
            _app_state(), chain, session, now=_NOW + timedelta(hours=1)
        )
    chain.get_weights.assert_not_awaited()
    async with maker() as session:
        reveal = (await get_king_reveal(session, agent_ids=[agent_id]))[agent_id]
    assert reveal.weight_confirmed_at == _NOW
