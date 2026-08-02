"""Real-Postgres convergence proof for concurrent rolling qualification."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.benchmark_rollout import (
    ensure_rolling_qualification,
    refresh_rolling_qualification,
)
from ditto.db import create_db_engine
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRolloutMember,
    Score,
    ValidatorHeartbeat,
)
from ditto.db.queries.benchmark_rollout import (
    CANARY_BENCH_VERSION,
    DEFAULT_BENCH_VERSION,
    DatasetPin,
    RolloutSnapshotMember,
    create_rollout_snapshot,
)
from ditto.tests.legacy_era import retired_era_writes_allowed

pytestmark = pytest.mark.integration


class _Generator:
    run_size = "full"

    async def generate(self, seed: int, bench_version: int = 2) -> str:
        assert seed == 41
        assert bench_version == CANARY_BENCH_VERSION
        await asyncio.sleep(0)
        return "e" * 64

    async def fetch_dataset(
        self, seed: int, run_size: str, bench_version: int = 2
    ) -> tuple[dict[str, Any], str]:
        del seed, run_size, bench_version
        raise AssertionError("not used by qualification")

    async def aclose(self) -> None:
        return None


async def test_concurrent_legacy_qualification_has_one_winner() -> None:
    """Two refreshes race over the same inherited cohort; exactly one wins.

    The inherited ledger here is v2 and cannot be renumbered forward. A rollout
    must move the version FORWARD to a target the contract registry ships, and
    the newest shipped contract is v7 -- so whatever era this qualifies FROM is
    below the retired-era floor by construction. These are grandfathered rows
    of exactly the kind production still holds, so they are written the way
    production got them: with the floor lifted for the seed and restored before
    the code under test runs.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    initial_ids = [uuid4() for _ in range(5)]
    rising_id = uuid4()
    async with maker() as session, retired_era_writes_allowed(session), session.begin():
        await session.execute(text("TRUNCATE TABLE benchmark_rollouts, agents CASCADE"))
        members: list[RolloutSnapshotMember] = []
        pins: dict = {}
        for position, agent_id in enumerate(initial_ids, start=1):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"miner-{position}",
                    name=f"agent-{position}",
                    sha256=f"{position:x}" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=9,
                    created_at=now + timedelta(seconds=position),
                )
            )
            members.append(
                RolloutSnapshotMember(
                    agent_id=agent_id,
                    miner_hotkey=f"miner-{position}",
                    composite=0.5 + position / 100,
                )
            )
            pins[agent_id] = DatasetPin(
                seed=position,
                sha256="c" * 64,
                run_size="full",
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=2,
                        validator_hotkey=f"legacy-{validator}",
                        run_id=f"v2-{position}-{validator}",
                        signature="aa",
                        seed=position,
                        composite=0.5 + position / 100,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
        session.add(
            Agent(
                agent_id=rising_id,
                miner_hotkey="miner-rising",
                name="legacy-rising",
                sha256="f" * 64,
                status=AgentStatus.SCORED,
                screening_policy_version=8,
                created_at=now + timedelta(minutes=1),
            )
        )
        for validator, seed in enumerate((43, 41, 42)):
            session.add_all(
                [
                    Score(
                        agent_id=rising_id,
                        bench_version=2,
                        validator_hotkey=f"rising-{validator}",
                        run_id=f"rising-{validator}",
                        signature="aa",
                        seed=seed,
                        composite=0.555,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    ),
                    Score(
                        agent_id=initial_ids[0],
                        bench_version=CANARY_BENCH_VERSION,
                        validator_hotkey=f"drop-{validator}",
                        run_id=f"drop-{validator}",
                        signature="bb",
                        seed=1,
                        composite=0.1,
                        tool_mean=0.1,
                        memory_mean=0.1,
                        median_ms=1,
                        n=114,
                        details={"bench_version": CANARY_BENCH_VERSION},
                        generated_at=now,
                    ),
                ]
            )
        await session.flush()
        # Explicit now that ``from_version`` has no default; this cohort is
        # the inherited pre-floor era, which is what the old default of 2 meant.
        rollout = await create_rollout_snapshot(
            session,
            members=members,
            datasets=pins,
            now=now,
            from_version=DEFAULT_BENCH_VERSION,
        )

    async with maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BenchmarkRolloutMember)
                .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            )
            == 5
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BenchmarkDataset)
                .where(BenchmarkDataset.bench_version == CANARY_BENCH_VERSION)
            )
            == 5
        )

    async def refresh() -> int:
        async with maker() as session:
            return await refresh_rolling_qualification(
                session, generator=_Generator(), now=now + timedelta(seconds=1)
            )

    assert sorted(await asyncio.gather(refresh(), refresh())) == [0, 1]
    async with maker() as session:
        member_count = await session.scalar(
            select(func.count())
            .select_from(BenchmarkRolloutMember)
            .where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.agent_id == rising_id,
            )
        )
        dataset_count = await session.scalar(
            select(func.count())
            .select_from(BenchmarkDataset)
            .where(
                BenchmarkDataset.agent_id == rising_id,
                BenchmarkDataset.bench_version == CANARY_BENCH_VERSION,
            )
        )
        dataset = await session.get(BenchmarkDataset, (rising_id, CANARY_BENCH_VERSION))
    assert member_count == 1
    assert dataset_count == 1
    assert dataset is not None and dataset.seed == 41
    await engine.dispose()


async def test_validator_bootstrap_is_disabled() -> None:
    """A fully qualified inherited cohort still opens no rollout on its own.

    The v2 scores are load-bearing: they are what would make these five agents
    qualify if bootstrap were still enabled, so renumbering them past the floor
    would empty the inherited ledger and make the assertion below pass for the
    wrong reason. They are seeded as the retired-era rows they are.
    """
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC).replace(microsecond=0)
    agent_ids = [uuid4() for _ in range(5)]
    async with maker() as session, retired_era_writes_allowed(session), session.begin():
        await session.execute(text("TRUNCATE TABLE benchmark_rollouts, agents CASCADE"))
        for position, agent_id in enumerate(agent_ids, start=1):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"bootstrap-miner-{position}",
                    name=f"bootstrap-{position}",
                    sha256=f"{position:x}" * 64,
                    status=AgentStatus.SCORED,
                    screening_policy_version=9,
                    dataset_seed=position,
                    dataset_sha256="c" * 64,
                    dataset_run_size="full",
                    created_at=now + timedelta(seconds=position),
                )
            )
            for validator in range(3):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=2,
                        validator_hotkey=f"bootstrap-validator-{validator}",
                        run_id=f"bootstrap-{position}-{validator}",
                        signature="aa",
                        seed=position,
                        composite=0.5 + position / 100,
                        tool_mean=0.5,
                        memory_mean=0.5,
                        median_ms=1,
                        n=114,
                        details={"bench_version": 2},
                        generated_at=now,
                    )
                )
        session.add(
            ValidatorHeartbeat(
                validator_hotkey="bootstrap-validator",
                software_version="1.0.0",
                protocol_version=8,
                code_digest="d" * 64,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                capabilities={},
                stack={},
            )
        )

    async with maker() as session:
        assert not await ensure_rolling_qualification(
            session, generator=_Generator(), now=now
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(BenchmarkRolloutMember)
            )
            == 0
        )
    await engine.dispose()
