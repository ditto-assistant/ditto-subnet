"""The carryover pass inside the rolling-qualification convergence loop.

Two properties are pinned here, and they are the ones that decide whether this
change is safe to merge:

1. With no settings revision written -- what a deployment that merely merges
   this actually has -- the pass is a total no-op. Nothing is selected, nothing
   is rendered, nothing is admitted, and the open rollout is untouched.
2. When an operator enables it, admission and dataset generation move together
   in one transaction. There is no window in which one exists without the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import (
    PrevGenCarryoverSettings,
    QueuePolicySettings,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.benchmark_rollout import refresh_rolling_qualification
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    Score,
)
from ditto.db.queries.benchmark_admission import benchmark_admission_predicate
from ditto.db.queries.benchmark_carryover import EVENT_CARRYOVER_ADOPTED
from ditto.db.queries.queue_policy_settings import (
    insert_queue_policy_settings_revision,
)
from ditto.tests.legacy_era import retired_era_writes_allowed

_ROLLOUT_START = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
_NOW = _ROLLOUT_START + timedelta(days=2)
# Not arbitrary, and not renumberable. Carryover only runs during a rollout,
# v7 is the newest shipped contract, and a rollout moves forward -- so the only
# transition this pass can ride is 6 -> 7, and the backlog it adopts is stranded
# on a RETIRED era by definition. The ``scores`` rows below are therefore rows
# the floor refuses to write today; production still holds its own because the
# constraint is NOT VALID. ``_seeding_the_retired_era`` reproduces that state.
_FROM_VERSION = 6
_DESIRED_VERSION = 7
_COHORT = 5


@asynccontextmanager
async def _seeding_the_retired_era(session: AsyncSession) -> AsyncIterator[None]:
    """Open the seeding transaction with the retired-era floor lifted.

    The backlog this pass exists to adopt is v6 work that never reached quorum.
    Writing it is exactly what ``scores_bench_version_floor`` stops now, so the
    floor comes off for the seed and goes straight back on -- NOT VALID, as the
    migration declares it -- before the pass under test runs. Everything
    ``refresh_rolling_qualification`` writes still meets the live floor.
    """
    async with retired_era_writes_allowed(session), session.begin():
        yield


def _generator() -> AsyncMock:
    generator = AsyncMock()
    generator.run_size = "full"
    generator.generate.return_value = "e" * 64
    return generator


async def _seed_agent(
    session: AsyncSession,
    *,
    name: str,
    score_count: int,
    created_at: datetime,
    status: AgentStatus = AgentStatus.EVALUATING,
) -> UUID:
    agent_id = uuid4()
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=f"5Miner-{name}",
        name=name,
        sha256=f"{abs(hash(name)) % (16**64):064x}",
        status=status,
        screening_policy_version=SCREENING_POLICY_VERSION,
        created_at=created_at,
    )
    agent.screened_image_sha256 = "12" * 32
    agent.screened_image_size_bytes = 123
    agent.screened_image_id = "sha256:" + "34" * 32
    agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
    agent.screened_image_upload_id = uuid4()
    agent.screened_image_verified_at = created_at
    session.add(agent)
    await session.flush()
    for index in range(score_count):
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=_FROM_VERSION,
                validator_hotkey=f"5Validator-{name}-{index}",
                run_id=f"{name}-{index}",
                signature=None,
                seed=11,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=114,
                details={"bench_version": _FROM_VERSION},
                generated_at=created_at,
            )
        )
    await session.flush()
    return agent_id


async def _seed_open_rollout(session: AsyncSession) -> BenchmarkRollout:
    """An open rollout whose inherited cohort is already frozen.

    The cohort is pre-frozen so the cohort pass has nothing left to do and any
    observed dataset render or membership change is unambiguously carryover's.
    """
    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=_FROM_VERSION,
        desired_version=_DESIRED_VERSION,
        status="collecting",
        cohort_size=_COHORT,
        created_at=_ROLLOUT_START,
        rescore_cohort_target=_COHORT,
        priority_cohort_target=_COHORT,
    )
    session.add(rollout)
    await session.flush()
    for position in range(1, _COHORT + 1):
        member_id = await _seed_agent(
            session,
            name=f"cohort-{position}",
            score_count=3,
            created_at=_ROLLOUT_START - timedelta(days=30),
            status=AgentStatus.SCORED,
        )
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout.rollout_id,
                agent_id=member_id,
                position=position,
                frozen_miner_hotkey=f"5Miner-cohort-{position}",
                frozen_composite=1.0 - position / 100,
            )
        )
    await session.flush()
    return rollout


async def _enable_carryover(session: AsyncSession, **overrides: object) -> None:
    settings = QueuePolicySettings(
        prev_gen_carryover=PrevGenCarryoverSettings(enabled=True, **overrides)  # type: ignore[arg-type]
    )
    await insert_queue_policy_settings_revision(
        session,
        parent_revision=0,
        scope="*",
        settings=settings.model_dump(mode="json"),
        checksum="a" * 64,
        reason="adopt the stranded bench-6 backlog into the v7 era",
        actor="peyton@omniaura.ai",
    )


async def _rollout_snapshot(session: AsyncSession) -> tuple[str, int]:
    rollout = (await session.scalars(select(BenchmarkRollout))).one()
    return rollout.status, rollout.cohort_size


class TestDisabledIsATotalNoOp:
    async def test_shipped_default_selects_renders_and_admits_nothing(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_open_rollout(session)
            stranded = await _seed_agent(
                session,
                name="stranded",
                score_count=2,
                created_at=_ROLLOUT_START - timedelta(days=5),
            )
            before = await _rollout_snapshot(session)

        generator = _generator()
        # No settings revision is written at all.
        await refresh_rolling_qualification(session, generator=generator, now=_NOW)

        async with session.begin():
            assert (
                list(await session.scalars(select(BenchmarkRolloutCarryover.agent_id)))
                == []
            )
            assert (
                await session.get(BenchmarkDataset, (stranded, _DESIRED_VERSION))
            ) is None
            admitted = set(
                await session.scalars(
                    select(Agent.agent_id).where(
                        benchmark_admission_predicate(
                            rollout=rollout, bench_version=_DESIRED_VERSION
                        )
                    )
                )
            )
            assert stranded not in admitted
            # The open rollout is byte-for-byte where it was.
            assert await _rollout_snapshot(session) == before
        generator.generate.assert_not_awaited()


class TestEnabledMovesAllThreeLegsTogether:
    async def test_row_and_dataset_are_created_in_one_pass(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            rollout = await _seed_open_rollout(session)
            stranded = await _seed_agent(
                session,
                name="stranded",
                score_count=2,
                created_at=_ROLLOUT_START - timedelta(days=5),
            )
            before = await _rollout_snapshot(session)
            await _enable_carryover(session)

        generator = _generator()
        await refresh_rolling_qualification(session, generator=generator, now=_NOW)

        async with session.begin():
            row = await session.get(
                BenchmarkRolloutCarryover, (rollout.rollout_id, stranded)
            )
            dataset = await session.get(BenchmarkDataset, (stranded, _DESIRED_VERSION))
            # Asserted together, because the point is that neither can exist
            # without the other.
            assert row is not None
            assert dataset is not None
            assert row.position == 1
            assert row.frozen_score_count == 2
            assert dataset.sha256 == "e" * 64
            assert dataset.seed == 11

            admitted = set(
                await session.scalars(
                    select(Agent.agent_id).where(
                        benchmark_admission_predicate(
                            rollout=rollout, bench_version=_DESIRED_VERSION
                        )
                    )
                )
            )
            assert stranded in admitted
            # Carryover still did not touch the transition it is riding on.
            assert await _rollout_snapshot(session) == before

            events = list(
                await session.scalars(
                    select(BenchmarkRolloutAudit.event).where(
                        BenchmarkRolloutAudit.rollout_id == rollout.rollout_id
                    )
                )
            )
            assert EVENT_CARRYOVER_ADOPTED in events
        generator.generate.assert_awaited_once_with(11, bench_version=_DESIRED_VERSION)

    async def test_repeated_passes_are_idempotent(self, session: AsyncSession) -> None:
        async with _seeding_the_retired_era(session):
            await _seed_open_rollout(session)
            await _seed_agent(
                session,
                name="stranded",
                score_count=2,
                created_at=_ROLLOUT_START - timedelta(days=5),
            )
            await _enable_carryover(session)

        for _ in range(3):
            await refresh_rolling_qualification(
                session, generator=_generator(), now=_NOW
            )

        async with session.begin():
            rows = list(
                await session.scalars(select(BenchmarkRolloutCarryover.agent_id))
            )
        assert len(rows) == 1

    async def test_the_cap_bounds_the_whole_backlog(
        self, session: AsyncSession
    ) -> None:
        async with _seeding_the_retired_era(session):
            await _seed_open_rollout(session)
            for index in range(4):
                await _seed_agent(
                    session,
                    name=f"stranded-{index}",
                    score_count=2,
                    created_at=_ROLLOUT_START - timedelta(days=5 + index),
                )
            await _enable_carryover(session, max_agents=2)

        await refresh_rolling_qualification(session, generator=_generator(), now=_NOW)

        async with session.begin():
            rows = list(
                await session.scalars(select(BenchmarkRolloutCarryover.position))
            )
        assert sorted(rows) == [1, 2]
