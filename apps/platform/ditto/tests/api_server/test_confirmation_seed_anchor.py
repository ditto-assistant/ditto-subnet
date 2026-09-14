"""The finalized-block binding of a reign's confirmation seed family.

Bench v13+ (``CRN_BLOCK_BINDING_MIN_BENCH_VERSION``) confirmation seeds hash a
Platform-pinned finalized block. These tests pin the lifecycle the lane relies
on: below the floor nothing is created; at the floor the first claim creates an
unpinned row at ``ready_block + Δ`` and plans no fresh seed; a chain that has
not finalized that height leaves it unpinned and the claim alive; finality pins
exactly once; and every issued seed of the version binds back to the reign that
introduced it, including a catch-up seed of an earlier reign.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server import crn as crn_mod
from ditto.api_server.confirmation_seed_anchor import (
    LEGACY_PLANNING,
    bind_confirmation_seed,
    list_reign_seed_anchors,
    reign_seed_planning,
    resolve_reign_seed_anchor,
)
from ditto.api_server.crn import (
    CRN_ANCHOR_BLOCK_DELTA,
    champion_anchored_seeds,
)
from ditto.chain import ChainConnectionError, ExtrinsicNotFoundError
from ditto.db.models import Agent, ConfirmationSeedAnchor

_HASH = "0x" + "ab" * 32
_OTHER_HASH = "0x" + "cd" * 32
_BOUND_VERSION = 13
_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


async def _seed_agent(maker: async_sessionmaker[AsyncSession], name: str) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner{name}",
                name=name,
                sha256=f"{len(name):02d}" * 32,
                status=AgentStatus.SCORED,
                created_at=_NOW,
            )
        )
    return agent_id


def _chain(*, finalized_hash: str | Exception) -> MagicMock:
    chain = MagicMock()
    if isinstance(finalized_hash, Exception):
        chain.get_finalized_block_hash = AsyncMock(side_effect=finalized_hash)
    else:
        chain.get_finalized_block_hash = AsyncMock(return_value=finalized_hash)
    return chain


async def test_legacy_version_never_creates_an_anchor(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "legacy")
    chain = _chain(finalized_hash=_HASH)
    async with session_maker() as session, session.begin():
        anchor = await resolve_reign_seed_anchor(
            session,
            chain,
            champion_agent_id=champion,
            bench_version=crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION - 1,
            ready_block=100,
            now=_NOW,
        )
        assert anchor is None
        assert (
            await reign_seed_planning(
                session,
                champion_agent_id=champion,
                bench_version=crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION - 1,
            )
            == LEGACY_PLANNING
        )
    chain.get_finalized_block_hash.assert_not_awaited()
    async with session_maker() as session:
        assert (await session.scalars(select(ConfirmationSeedAnchor))).all() == []


async def test_first_claim_creates_an_unpinned_anchor_at_ready_plus_delta(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "fresh")
    chain = _chain(finalized_hash=ExtrinsicNotFoundError("block 110 is not finalized"))
    async with session_maker() as session, session.begin():
        anchor = await resolve_reign_seed_anchor(
            session,
            chain,
            champion_agent_id=champion,
            bench_version=_BOUND_VERSION,
            ready_block=100,
            now=_NOW,
        )
    assert anchor is not None
    assert not anchor.pinned
    assert anchor.ready_block == 100
    assert anchor.anchor_block == 100 + CRN_ANCHOR_BLOCK_DELTA
    # The finality wait: no fresh seed may be planned while unpinned.
    assert anchor.planning == (None, False)
    chain.get_finalized_block_hash.assert_awaited_once_with(
        100 + CRN_ANCHOR_BLOCK_DELTA
    )
    async with session_maker() as session:
        assert (
            await reign_seed_planning(
                session, champion_agent_id=champion, bench_version=_BOUND_VERSION
            )
        ) == (None, False)
        assert (
            await list_reign_seed_anchors(session, bench_version=_BOUND_VERSION) == []
        )


async def test_chain_trouble_leaves_the_anchor_unpinned_and_the_claim_alive(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "outage")
    chain = _chain(finalized_hash=ChainConnectionError("substrate unreachable"))
    async with session_maker() as session, session.begin():
        anchor = await resolve_reign_seed_anchor(
            session,
            chain,
            champion_agent_id=champion,
            bench_version=_BOUND_VERSION,
            ready_block=7,
            now=_NOW,
        )
    assert anchor is not None and not anchor.pinned


async def test_finality_pins_once_and_later_claims_keep_the_ready_block(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "reign")
    waiting = _chain(finalized_hash=ExtrinsicNotFoundError("not finalized"))
    async with session_maker() as session, session.begin():
        await resolve_reign_seed_anchor(
            session,
            waiting,
            champion_agent_id=champion,
            bench_version=_BOUND_VERSION,
            ready_block=100,
            now=_NOW,
        )
    # A later claim observes a newer head; the anchor must not move with it.
    finalized = _chain(finalized_hash=_HASH)
    async with session_maker() as session, session.begin():
        pinned = await resolve_reign_seed_anchor(
            session,
            finalized,
            champion_agent_id=champion,
            bench_version=_BOUND_VERSION,
            ready_block=250,
            now=_NOW,
        )
    assert pinned is not None
    assert pinned.ready_block == 100
    assert pinned.anchor_block == 100 + CRN_ANCHOR_BLOCK_DELTA
    assert pinned.block_hash == _HASH
    assert pinned.planning == (_HASH, True)
    # Pinned exactly once: a third claim neither re-reads the chain nor re-pins.
    drifted = _chain(finalized_hash=_OTHER_HASH)
    async with session_maker() as session, session.begin():
        again = await resolve_reign_seed_anchor(
            session,
            drifted,
            champion_agent_id=champion,
            bench_version=_BOUND_VERSION,
            ready_block=999,
            now=_NOW,
        )
    assert again is not None and again.block_hash == _HASH
    drifted.get_finalized_block_hash.assert_not_awaited()
    async with session_maker() as session:
        row = await session.get(ConfirmationSeedAnchor, (champion, _BOUND_VERSION))
        assert row is not None
        assert row.anchor_block_hash == _HASH
        assert row.pinned_at is not None
        anchors = await list_reign_seed_anchors(session, bench_version=_BOUND_VERSION)
    assert [anchor.champion_agent_id for anchor in anchors] == [champion]


async def test_every_issued_seed_binds_back_to_the_reign_that_introduced_it(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_agent(session_maker, "first")
    second = await _seed_agent(session_maker, "second")
    async with session_maker() as session, session.begin():
        await resolve_reign_seed_anchor(
            session,
            _chain(finalized_hash=_HASH),
            champion_agent_id=first,
            bench_version=_BOUND_VERSION,
            ready_block=100,
            now=_NOW,
        )
        await resolve_reign_seed_anchor(
            session,
            _chain(finalized_hash=_OTHER_HASH),
            champion_agent_id=second,
            bench_version=_BOUND_VERSION,
            ready_block=500,
            now=_NOW,
        )
    async with session_maker() as session:
        anchors = await list_reign_seed_anchors(session, bench_version=_BOUND_VERSION)
    assert [anchor.champion_agent_id for anchor in anchors] == [first, second]

    first_family = champion_anchored_seeds(
        first, version=_BOUND_VERSION, max_seeds=15, block_hash=_HASH
    )
    second_family = champion_anchored_seeds(
        second, version=_BOUND_VERSION, max_seeds=15, block_hash=_OTHER_HASH
    )
    catchup = bind_confirmation_seed(
        anchors, seed=first_family[4], bench_version=_BOUND_VERSION
    )
    assert catchup is not None
    assert catchup.anchor_agent_id == first
    assert catchup.seed_index == 4
    assert catchup.seed_block == 100 + CRN_ANCHOR_BLOCK_DELTA
    assert catchup.seed_block_hash == _HASH
    growth = bind_confirmation_seed(
        anchors, seed=second_family[0], bench_version=_BOUND_VERSION
    )
    assert growth is not None
    assert growth.anchor_agent_id == second
    assert growth.seed_block_hash == _OTHER_HASH
    # A seed no pinned reign derives -- the unbound legacy family -- is unbound.
    legacy_seed = champion_anchored_seeds(first, version=_BOUND_VERSION, max_seeds=1)[0]
    assert (
        bind_confirmation_seed(anchors, seed=legacy_seed, bench_version=_BOUND_VERSION)
        is None
    )
    # And nothing binds below the floor, whatever anchors exist.
    assert (
        bind_confirmation_seed(
            anchors,
            seed=first_family[0],
            bench_version=crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION - 1,
        )
        is None
    )


def test_binding_floor_can_be_read_as_a_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lane tests lower the floor to the fixture era; that only works because
    # every gate reads the constant at call time instead of freezing it.
    monkeypatch.setattr(crn_mod, "CRN_BLOCK_BINDING_MIN_BENCH_VERSION", 7)
    assert crn_mod.crn_block_binding_active(7)
    assert not crn_mod.crn_block_binding_active(6)
