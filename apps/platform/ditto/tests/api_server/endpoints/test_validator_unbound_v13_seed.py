"""A block-less V13 dataset pin is rebound to the chain at lease time.

A chain blip at screening pins a local CSPRNG seed with null block columns.
V13 leases derive every validator's seed from the block hash, so that pin used
to 503 every poll, and as the queue head it rolled back the whole fleet's
dispatch (2026-10-16, Harry v19 stranded six agents for 38 hours).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.datapipeline import DataPipelineError
from ditto.api_server.endpoints.validator import _bind_unverified_v13_dataset
from ditto.api_server.onchain_seed import derive_seed
from ditto.chain import ChainError
from ditto.chain.models import BlockInfo
from ditto.db.models import (
    Agent,
    AgentStatus,
    BenchmarkDataset,
    TicketPurpose,
    TicketStatus,
    ValidatorTicket,
)

_BLOCK_HASH = "0x" + "cd" * 32
_CSPRNG_SEED = 123_456_789


class _Chain:
    def __init__(self, result: BlockInfo | Exception) -> None:
        self._result = result

    async def get_latest_block(self) -> BlockInfo:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Generator:
    run_size = "full"

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def generate(self, seed: int, bench_version: int) -> str:
        if self._fail:
            raise DataPipelineError("generator down")
        return hashlib.sha256(f"{bench_version}:{seed}".encode()).hexdigest()


async def _seed_blockless(
    session_maker: async_sessionmaker[AsyncSession],
) -> tuple[Agent, BenchmarkDataset]:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5HKsomething",
        name="harry",
        sha256="deadbeef" * 8,
        status=AgentStatus.EVALUATING,
        created_at=datetime(2026, 10, 16, 6, 37, tzinfo=UTC),
        dataset_seed=_CSPRNG_SEED,
        dataset_sha256="0" * 64,
        dataset_run_size="full",
    )
    dataset = BenchmarkDataset(
        agent_id=agent.agent_id,
        bench_version=13,
        seed=_CSPRNG_SEED,
        sha256="0" * 64,
        run_size="full",
        seed_block=None,
        seed_block_hash=None,
    )
    async with session_maker() as session, session.begin():
        session.add(agent)
        await session.flush()
        session.add(dataset)
    return agent, dataset


async def _bind(
    session: AsyncSession, agent_id: Any, *, chain: _Chain, generator: _Generator
) -> bool:
    agent = await session.get(Agent, agent_id)
    dataset = await session.get(BenchmarkDataset, (agent_id, 13))
    assert agent is not None and dataset is not None
    return await _bind_unverified_v13_dataset(
        session,
        chain=cast(Any, chain),
        request=cast(Request, None),
        generator=cast(Any, generator),
        agent=agent,
        dataset=dataset,
    )


async def test_blockless_pin_is_rebound_to_the_latest_block(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent, _ = await _seed_blockless(session_maker)
    chain = _Chain(BlockInfo(number=9_305_000, hash=_BLOCK_HASH))

    async with session_maker() as session, session.begin():
        assert await _bind(session, agent.agent_id, chain=chain, generator=_Generator())

    expected_seed = derive_seed(_BLOCK_HASH, agent.agent_id)
    expected_sha = hashlib.sha256(f"13:{expected_seed}".encode()).hexdigest()
    async with session_maker() as session:
        dataset = await session.get(BenchmarkDataset, (agent.agent_id, 13))
        stored = await session.get(Agent, agent.agent_id)
    assert dataset is not None and stored is not None
    assert (dataset.seed, dataset.sha256) == (expected_seed, expected_sha)
    assert (dataset.seed_block, dataset.seed_block_hash) == (9_305_000, _BLOCK_HASH)
    # The compatibility columns mirrored the same block-less pin, so they follow
    # and the public derive_seed(hash, agent_id) check verifies again.
    assert stored.dataset_seed == expected_seed
    assert stored.dataset_seed_block_hash == _BLOCK_HASH


async def test_a_pin_a_lease_already_derived_from_is_never_rebound(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent, _ = await _seed_blockless(session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent.agent_id,
                validator_hotkey="5Validator",
                slot_id="slot-0",
                status=TicketStatus.SCORED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=now - timedelta(hours=2),
                deadline=now - timedelta(hours=1),
                bench_version=13,
                attempt_count=1,
                manual_retry_grants=0,
                seed=42,
            )
        )
    chain = _Chain(BlockInfo(number=9_305_000, hash=_BLOCK_HASH))

    async with session_maker() as session, session.begin():
        assert not await _bind(
            session, agent.agent_id, chain=chain, generator=_Generator()
        )

    async with session_maker() as session:
        dataset = await session.get(BenchmarkDataset, (agent.agent_id, 13))
    assert dataset is not None
    assert (dataset.seed, dataset.seed_block_hash) == (_CSPRNG_SEED, None)


@pytest.mark.parametrize(
    ("chain", "generator"),
    [
        (_Chain(ChainError("pylon down")), _Generator()),
        (_Chain(BlockInfo(number=1, hash=_BLOCK_HASH)), _Generator(fail=True)),
    ],
    ids=["chain_down", "generator_down"],
)
async def test_unavailable_dependencies_leave_the_pin_untouched(
    session_maker: async_sessionmaker[AsyncSession],
    chain: _Chain,
    generator: _Generator,
) -> None:
    agent, _ = await _seed_blockless(session_maker)

    async with session_maker() as session, session.begin():
        assert not await _bind(
            session, agent.agent_id, chain=chain, generator=generator
        )

    async with session_maker() as session:
        dataset = await session.get(BenchmarkDataset, (agent.agent_id, 13))
    assert dataset is not None
    assert (dataset.seed, dataset.seed_block_hash) == (_CSPRNG_SEED, None)
