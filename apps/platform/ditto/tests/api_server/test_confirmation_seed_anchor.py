"""The finalized-block binding of a reign's confirmation seed family.

Bench v13+ (``CRN_BLOCK_BINDING_MIN_BENCH_VERSION``) confirmation seeds hash a
Platform-pinned finalized block. These tests pin the lifecycle the lane relies
on: below the floor nothing is created; at the floor the first claim creates an
unpinned row at ``ready_block + Δ`` and plans no fresh seed; a chain that has
not finalized that height leaves it unpinned and the claim alive; finality pins
exactly once; the chain is read only outside a write transaction and only
within a bounded timeout; and every issued seed of the version binds back to
the reign that introduced it, including a catch-up seed of an earlier reign.
"""

from __future__ import annotations

import asyncio
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
    ReignSeedAnchor,
    bind_confirmation_seed,
    list_reign_seed_anchors,
    prefetch_finalized_anchor_hashes,
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


async def _claim(
    maker: async_sessionmaker[AsyncSession],
    chain: MagicMock,
    *,
    champion: UUID,
    version: int = _BOUND_VERSION,
    ready_block: int,
) -> ReignSeedAnchor | None:
    """One claim as the endpoint performs it: prefetch outside, pin inside."""
    async with maker() as session:
        finalized = await prefetch_finalized_anchor_hashes(
            session, chain, latest_block=ready_block
        )
        async with session.begin():
            return await resolve_reign_seed_anchor(
                session,
                champion_agent_id=champion,
                bench_version=version,
                ready_block=ready_block,
                now=_NOW,
                finalized_hashes=finalized,
            )


async def test_legacy_version_never_creates_an_anchor(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "legacy")
    chain = _chain(finalized_hash=_HASH)
    anchor = await _claim(
        session_maker,
        chain,
        champion=champion,
        version=crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION - 1,
        ready_block=100,
    )
    assert anchor is None
    async with session_maker() as session:
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
    chain = _chain(finalized_hash=_HASH)
    anchor = await _claim(session_maker, chain, champion=champion, ready_block=100)
    assert anchor is not None
    assert not anchor.pinned
    assert anchor.ready_block == 100
    assert anchor.anchor_block == 100 + CRN_ANCHOR_BLOCK_DELTA
    # The finality wait: no fresh seed may be planned while unpinned.
    assert anchor.planning == (None, False)
    # Nothing was waiting before this claim, so the chain was never asked: the
    # anchor height is in the future for everyone, Platform included.
    chain.get_finalized_block_hash.assert_not_awaited()
    async with session_maker() as session:
        assert (
            await reign_seed_planning(
                session, champion_agent_id=champion, bench_version=_BOUND_VERSION
            )
        ) == (None, False)
        assert (
            await list_reign_seed_anchors(session, bench_version=_BOUND_VERSION) == []
        )


async def test_head_below_the_anchor_height_reads_no_chain(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "early")
    await _claim(
        session_maker, _chain(finalized_hash=_HASH), champion=champion, ready_block=100
    )
    chain = _chain(finalized_hash=_HASH)
    # Head at 105 < anchor 110: the row cannot be finalized yet, so no RPC.
    anchor = await _claim(session_maker, chain, champion=champion, ready_block=105)
    assert anchor is not None and not anchor.pinned
    chain.get_finalized_block_hash.assert_not_awaited()


async def test_chain_trouble_leaves_the_anchor_unpinned_and_the_claim_alive(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "outage")
    await _claim(
        session_maker, _chain(finalized_hash=_HASH), champion=champion, ready_block=7
    )
    for trouble in (
        ChainConnectionError("substrate unreachable"),
        ExtrinsicNotFoundError("block 17 is not finalized"),
    ):
        chain = _chain(finalized_hash=trouble)
        anchor = await _claim(session_maker, chain, champion=champion, ready_block=40)
        assert anchor is not None and not anchor.pinned
        chain.get_finalized_block_hash.assert_awaited_once_with(
            7 + CRN_ANCHOR_BLOCK_DELTA
        )


async def test_a_slow_chain_read_is_bounded_and_treated_as_not_pinned(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The finalized-hash read runs under ``asyncio.wait_for``: a hung Substrate
    endpoint costs one claim the timeout, never a held transaction."""
    champion = await _seed_agent(session_maker, "slow")
    await _claim(
        session_maker, _chain(finalized_hash=_HASH), champion=champion, ready_block=100
    )

    async def _hang(_block: int) -> str:
        await asyncio.sleep(30)
        return _HASH

    chain = MagicMock()
    chain.get_finalized_block_hash = AsyncMock(side_effect=_hang)
    async with session_maker() as session:
        finalized = await prefetch_finalized_anchor_hashes(
            session, chain, latest_block=200, timeout=0.01
        )
        # No transaction is open once the prefetch returns.
        assert not session.in_transaction()
    assert finalized == {}
    async with session_maker() as session:
        row = await session.get(ConfirmationSeedAnchor, (champion, _BOUND_VERSION))
        assert row is not None and row.anchor_block_hash is None


async def test_prefetch_is_bounded_per_claim_and_skips_pinned_rows(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champions = [await _seed_agent(session_maker, f"reign{i}") for i in range(3)]
    for index, champion in enumerate(champions):
        await _claim(
            session_maker,
            _chain(finalized_hash=_HASH),
            champion=champion,
            ready_block=100 + index,
        )
    chain = _chain(finalized_hash=_HASH)
    async with session_maker() as session:
        finalized = await prefetch_finalized_anchor_hashes(
            session, chain, latest_block=500, limit=2
        )
    # Oldest anchors first, at most ``limit`` reads.
    assert set(finalized) == {
        (champions[0], _BOUND_VERSION),
        (champions[1], _BOUND_VERSION),
    }
    assert chain.get_finalized_block_hash.await_count == 2
    # Pin the first two; the third is the only one still waiting.
    for champion in champions[:2]:
        await _claim(
            session_maker,
            _chain(finalized_hash=_HASH),
            champion=champion,
            ready_block=500,
        )
    chain = _chain(finalized_hash=_OTHER_HASH)
    async with session_maker() as session:
        finalized = await prefetch_finalized_anchor_hashes(
            session, chain, latest_block=500
        )
    assert finalized == {(champions[2], _BOUND_VERSION): _OTHER_HASH}
    chain.get_finalized_block_hash.assert_awaited_once_with(
        102 + CRN_ANCHOR_BLOCK_DELTA
    )


async def test_finality_pins_once_and_later_claims_keep_the_ready_block(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    champion = await _seed_agent(session_maker, "reign")
    await _claim(
        session_maker,
        _chain(finalized_hash=ExtrinsicNotFoundError("not finalized")),
        champion=champion,
        ready_block=100,
    )
    # A later claim observes a newer head; the anchor must not move with it.
    finalized = _chain(finalized_hash=_HASH)
    pinned = await _claim(session_maker, finalized, champion=champion, ready_block=250)
    assert pinned is not None
    assert pinned.ready_block == 100
    assert pinned.anchor_block == 100 + CRN_ANCHOR_BLOCK_DELTA
    assert pinned.block_hash == _HASH
    assert pinned.planning == (_HASH, True)
    finalized.get_finalized_block_hash.assert_awaited_once_with(
        100 + CRN_ANCHOR_BLOCK_DELTA
    )
    # Pinned exactly once: a third claim neither re-reads the chain nor re-pins.
    drifted = _chain(finalized_hash=_OTHER_HASH)
    again = await _claim(session_maker, drifted, champion=champion, ready_block=999)
    assert again is not None and again.block_hash == _HASH
    drifted.get_finalized_block_hash.assert_not_awaited()
    async with session_maker() as session:
        row = await session.get(ConfirmationSeedAnchor, (champion, _BOUND_VERSION))
        assert row is not None
        assert row.anchor_block_hash == _HASH
        assert row.pinned_at is not None
        anchors = await list_reign_seed_anchors(session, bench_version=_BOUND_VERSION)
    assert [anchor.champion_agent_id for anchor in anchors] == [champion]


async def test_a_prefetched_hash_for_another_reign_pins_nothing_here(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The pin keys on the exact reign: a hash read for one champion can never
    be written onto another champion's anchor."""
    first = await _seed_agent(session_maker, "first")
    second = await _seed_agent(session_maker, "second")
    async with session_maker() as session, session.begin():
        anchor = await resolve_reign_seed_anchor(
            session,
            champion_agent_id=second,
            bench_version=_BOUND_VERSION,
            ready_block=100,
            now=_NOW,
            finalized_hashes={(first, _BOUND_VERSION): _HASH},
        )
    assert anchor is not None and not anchor.pinned


async def test_every_issued_seed_binds_back_to_the_reign_that_introduced_it(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed_agent(session_maker, "first")
    second = await _seed_agent(session_maker, "second")
    async with session_maker() as session, session.begin():
        await resolve_reign_seed_anchor(
            session,
            champion_agent_id=first,
            bench_version=_BOUND_VERSION,
            ready_block=100,
            now=_NOW,
            finalized_hashes={(first, _BOUND_VERSION): _HASH},
        )
        await resolve_reign_seed_anchor(
            session,
            champion_agent_id=second,
            bench_version=_BOUND_VERSION,
            ready_block=500,
            now=_NOW,
            finalized_hashes={(second, _BOUND_VERSION): _OTHER_HASH},
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


def test_binding_constants_are_the_shared_protocol_values() -> None:
    """Both CRN copies import the floor, delta, and normalizer from
    ``ditto_screening_protocol.crn_block_binding``; a retyped literal here
    would be the exact drift the bump skill forbids."""
    from ditto_screening_protocol import crn_block_binding as shared

    assert (
        crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION
        == shared.CRN_BLOCK_BINDING_MIN_BENCH_VERSION
    )
    assert crn_mod.CRN_ANCHOR_BLOCK_DELTA == shared.CRN_ANCHOR_BLOCK_DELTA
    assert crn_mod.normalize_block_hash is shared.normalize_block_hash
