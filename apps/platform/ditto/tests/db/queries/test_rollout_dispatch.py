"""Real-Postgres interleavings for the global dispatch fence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, Score
from ditto.db.queries.rollout_dispatch import (
    ROLLOUT_DISPATCH_LOCK_KEY,
    try_lock_rollout_dispatch,
)
from ditto.db.queries.scores import upsert_score


async def test_dispatch_fence_is_fail_fast_and_does_not_block_settlement(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as seed, seed.begin():
        seed.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5SettlementMiner",
                name="dispatch-settlement",
                sha256="ab" * 32,
                size_bytes=1,
                status=AgentStatus.EVALUATING,
                screening_policy_version=9,
                created_at=now,
            )
        )

    async with session_maker() as holder:
        await holder.begin()
        assert await try_lock_rollout_dispatch(holder)

        async with session_maker() as contender, contender.begin():
            assert not await asyncio.wait_for(
                try_lock_rollout_dispatch(contender), timeout=0.2
            )

        async def settle() -> None:
            async with session_maker() as session, session.begin():
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey="5SettlementValidator",
                    bench_version=7,
                    run_id="settled-while-dispatch-busy",
                    seed=42,
                    composite=0.8,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=100,
                    n=114,
                    generated_at=now,
                )

        await asyncio.wait_for(settle(), timeout=0.5)
        assert await holder.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": ROLLOUT_DISPATCH_LOCK_KEY},
        )
        await holder.rollback()

    async with session_maker() as probe:
        assert (
            await probe.scalar(
                select(func.count())
                .select_from(Score)
                .where(
                    Score.agent_id == agent_id,
                    Score.run_id == "settled-while-dispatch-busy",
                )
            )
            == 1
        )
