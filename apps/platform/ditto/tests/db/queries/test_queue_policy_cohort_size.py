"""The operator-configurable rescore cohort size and its freeze semantics.

Four things are pinned here, one per requirement of the design:

1. Bounds are ``5 <= n <= 25`` at the query layer, and the wire model's
   constants agree with the query module's.
2. A rollout freezes its size at START; a later policy revision never resizes
   an in-flight rollout.
3. The frozen size is recoverable from the rollout row and from the audit trail.
4. Widening the cohort never makes the historical backfill reach past the
   documented two previous benchmark iterations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import (
    DEFAULT_RESCORE_COHORT_SIZE as WIRE_DEFAULT,
)
from ditto.api_models.queue_policy_settings import (
    MAX_COHORT_SIZE as WIRE_MAX,
)
from ditto.api_models.queue_policy_settings import (
    MIN_COHORT_SIZE as WIRE_MIN,
)
from ditto.api_models.queue_policy_settings import (
    QueuePolicySettings,
)
from ditto.api_server.benchmark_rollout import _rollout_rescore_cohort
from ditto.api_server.queue_policy_settings import (
    resolve_queue_policy_settings,
)
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutMember,
    Score,
)
from ditto.db.queries.benchmark_rollout import (
    DEFAULT_RESCORE_COHORT_SIZE,
    MAX_PERSISTED_RESCORE_COHORT_SIZE,
    PRIORITY_COHORT_SIZE,
    DatasetPin,
    RolloutSnapshotMember,
    create_rollout_snapshot,
    historical_rescore_cohort,
)
from ditto.db.queries.queue_policy_settings import (
    insert_queue_policy_settings_revision,
)

# asyncio_mode = "auto" (pyproject) handles the async tests; a module-level
# asyncio mark would warn on the synchronous validation tests below.
_NOW = datetime.now(UTC).replace(microsecond=0)


async def _seed_era(
    session: AsyncSession, *, version: int, count: int, offset: int
) -> list[UUID]:
    """``count`` fully finalized, distinctly-owned agents scored on ``version``."""
    agent_ids: list[UUID] = []
    for rank in range(count):
        agent_id = uuid4()
        agent_ids.append(agent_id)
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"miner-v{version}-{rank}",
                name=f"agent-v{version}-{rank}",
                sha256=f"{offset + rank:064x}",
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                created_at=_NOW + timedelta(seconds=version * 1000 + rank),
            )
        )
        for validator in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=version,
                    validator_hotkey=f"validator-{version}-{validator}",
                    run_id=f"run-{version}-{rank}-{validator}",
                    signature="aa",
                    seed=rank,
                    composite=1 - rank / 1000,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=1,
                    n=114,
                    details={"bench_version": version},
                    generated_at=_NOW,
                )
            )
    await session.flush()
    return agent_ids


def _members(agent_ids: list[UUID]) -> list[RolloutSnapshotMember]:
    return [
        RolloutSnapshotMember(agent_id, f"hotkey-{index}", 1 - index / 1000)
        for index, agent_id in enumerate(agent_ids)
    ]


def _pins(members: list[RolloutSnapshotMember]) -> dict[UUID, DatasetPin]:
    return {
        member.agent_id: DatasetPin(seed=index, sha256=f"{index:064x}", run_size="full")
        for index, member in enumerate(members)
    }


class TestBounds:
    def test_wire_constants_match_query_constants(self) -> None:
        # The wire model spells the bounds out rather than importing them
        # (ditto.db.queries imports ditto.api_models, so importing back would
        # be a cycle). This is the guard against the two drifting apart.
        assert WIRE_MIN == PRIORITY_COHORT_SIZE
        assert WIRE_MAX == MAX_PERSISTED_RESCORE_COHORT_SIZE
        assert WIRE_DEFAULT == DEFAULT_RESCORE_COHORT_SIZE

    def test_settings_default_is_the_historical_ten(self) -> None:
        assert QueuePolicySettings().rescore_cohort_size == 10

    @pytest.mark.parametrize("size", [4, 0, -1, 26, 100])
    def test_settings_reject_out_of_range(self, size: int) -> None:
        with pytest.raises(ValueError):
            QueuePolicySettings(rescore_cohort_size=size)

    @pytest.mark.parametrize("size", [5, 10, 25])
    def test_settings_accept_in_range(self, size: int) -> None:
        assert QueuePolicySettings(rescore_cohort_size=size)

    @pytest.mark.parametrize("limit", [4, 26])
    async def test_historical_cohort_rejects_out_of_range_limit(
        self, session: AsyncSession, limit: int
    ) -> None:
        with pytest.raises(ValueError, match="between 5 and 25"):
            await historical_rescore_cohort(session, source_version=7, limit=limit)

    async def test_historical_cohort_accepts_the_ceiling(
        self, session: AsyncSession
    ) -> None:
        # The pre-change bound was the default ten, so raising the operator
        # setting to twenty-five would have thrown here immediately.
        assert (
            await historical_rescore_cohort(
                session,
                source_version=7,
                limit=MAX_PERSISTED_RESCORE_COHORT_SIZE,
            )
            == []
        )


class TestHistoricalBackfill:
    async def test_widening_still_reads_exactly_two_prior_eras(
        self, session: AsyncSession
    ) -> None:
        """Requirement 4: the ceiling cannot reach into a third benchmark era.

        Six v9 agents and six v8 agents cannot fill twenty-five slots. If the
        limit drove how far back the fallback reached, the v7 era would be
        pulled in to make up the difference. It must not be.

        The three eras are 9/8/7 rather than the 4/3/2 this was first written
        against: the bench-version floor refuses to write a score below 7 at
        all, and the rule under test is about adjacency, not about which
        absolute versions happen to be current.
        """
        async with session.begin():
            v9 = await _seed_era(session, version=9, count=6, offset=9000)
            v8 = await _seed_era(session, version=8, count=6, offset=8000)
            await _seed_era(session, version=7, count=10, offset=7000)

            cohort = await historical_rescore_cohort(
                session,
                source_version=9,
                limit=MAX_PERSISTED_RESCORE_COHORT_SIZE,
            )

        assert [member.agent_id for member in cohort] == [*v9, *v8]
        assert len(cohort) == 12
        assert not any("v7" in member.miner_hotkey for member in cohort)

    async def test_widening_admits_more_of_the_previous_era(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            await _seed_era(session, version=7, count=20, offset=7000)
            ten = await historical_rescore_cohort(session, source_version=7, limit=10)
            twenty_five = await historical_rescore_cohort(
                session, source_version=7, limit=25
            )
        assert len(ten) == 10
        assert len(twenty_five) == 20
        # Widening only appends: the top ten keep their identity and order, so
        # a rollout started at 25 rescores a superset of the one started at 10.
        assert twenty_five[:10] == ten


class TestSnapshotFreezesTheTarget:
    async def test_snapshot_persists_the_target_and_audits_it(
        self, session: AsyncSession
    ) -> None:
        """Requirement 3: the effective size is recoverable after the fact."""
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=12, offset=7000)
            members = _members(agent_ids)
            rollout = await create_rollout_snapshot(
                session,
                members=members,
                datasets=_pins(members),
                now=_NOW,
                from_version=7,
                desired_version=8,
                rescore_cohort_target=25,
            )
            rollout_id = rollout.rollout_id

        stored = await session.get(BenchmarkRollout, rollout_id)
        assert stored is not None
        assert stored.rescore_cohort_target == 25
        # cohort_size is how many were actually frozen; the target is what the
        # rollout was built to. The two are deliberately independent.
        assert stored.cohort_size == 12

        audit = (
            await session.scalars(
                select(BenchmarkRolloutAudit).where(
                    BenchmarkRolloutAudit.rollout_id == rollout_id
                )
            )
        ).all()
        assert len(audit) == 1
        assert audit[0].event == "cohort_frozen"

    async def test_snapshot_defaults_to_ten(self, session: AsyncSession) -> None:
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=6, offset=7000)
            members = _members(agent_ids)
            rollout = await create_rollout_snapshot(
                session,
                members=members,
                datasets=_pins(members),
                now=_NOW,
                from_version=7,
                desired_version=8,
            )
            assert rollout.rescore_cohort_target == DEFAULT_RESCORE_COHORT_SIZE

    @pytest.mark.parametrize("target", [4, 26])
    async def test_snapshot_rejects_out_of_range_target(
        self, session: AsyncSession, target: int
    ) -> None:
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=6, offset=7000)
            members = _members(agent_ids)
            with pytest.raises(ValueError, match="between 5 and 25"):
                await create_rollout_snapshot(
                    session,
                    members=members,
                    datasets=_pins(members),
                    now=_NOW,
                    from_version=7,
                    desired_version=8,
                    rescore_cohort_target=target,
                )

    async def test_snapshot_rejects_more_members_than_the_target(
        self, session: AsyncSession
    ) -> None:
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=12, offset=7000)
            members = _members(agent_ids)
            with pytest.raises(ValueError, match="between 5 and 10 members"):
                await create_rollout_snapshot(
                    session,
                    members=members,
                    datasets=_pins(members),
                    now=_NOW,
                    from_version=7,
                    desired_version=8,
                    rescore_cohort_target=10,
                )


class TestMidRolloutChangeIsIgnored:
    """Requirement 1: an open rollout keeps the size it froze at start."""

    async def test_raising_the_policy_does_not_grow_an_open_cohort(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_maker() as session:
            async with session.begin():
                agent_ids = await _seed_era(session, version=7, count=20, offset=7000)
                members = _members(agent_ids[:10])
                rollout = await create_rollout_snapshot(
                    session,
                    members=members,
                    datasets=_pins(members),
                    now=_NOW,
                    from_version=7,
                    desired_version=8,
                    rescore_cohort_target=10,
                )
                rollout_id = rollout.rollout_id

            # The operator widens the policy to the ceiling mid-rollout.
            async with session.begin():
                await insert_queue_policy_settings_revision(
                    session,
                    parent_revision=0,
                    scope="*",
                    settings={"rescore_cohort_size": 25},
                    checksum="b" * 64,
                    reason="the subnet is scaling; widen the rescore cohort",
                    actor="peyton@omniaura.ai",
                )
                assert (
                    await resolve_queue_policy_settings(session)
                ).rescore_cohort_size == 25

            async with session.begin():
                reloaded = await session.get(BenchmarkRollout, rollout_id)
                assert reloaded is not None
                cohort = await _rollout_rescore_cohort(session, rollout=reloaded)

            # Still ten. The new value applies to the NEXT rollout only.
            assert len(cohort) == 10
            assert reloaded.rescore_cohort_target == 10
            persisted = (
                await session.scalars(
                    select(BenchmarkRolloutMember).where(
                        BenchmarkRolloutMember.rollout_id == rollout_id
                    )
                )
            ).all()
            assert len(persisted) == 10

    async def test_a_rollout_frozen_at_the_ceiling_backfills_to_it(
        self, session: AsyncSession
    ) -> None:
        """The converse: a rollout that DID freeze 25 fills all the way up.

        Proves the freeze is a real per-rollout value and not a disguised
        constant -- an open rollout targeting 25 keeps qualifying members past
        the historical default of ten.
        """
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=20, offset=7000)
            members = _members(agent_ids[:5])
            rollout = await create_rollout_snapshot(
                session,
                members=members,
                datasets=_pins(members),
                now=_NOW,
                from_version=7,
                desired_version=8,
                rescore_cohort_target=25,
            )
            cohort = await _rollout_rescore_cohort(session, rollout=rollout)

        # Five already frozen plus the rest of the eligible v4 era.
        assert len(cohort) == 20

    async def test_database_refuses_a_target_above_the_ceiling(
        self, session: AsyncSession
    ) -> None:
        """The CHECK constraint is the primary guard against a bad target."""
        with pytest.raises(IntegrityError):
            async with session.begin():
                agent_ids = await _seed_era(session, version=7, count=6, offset=7000)
                members = _members(agent_ids)
                rollout = await create_rollout_snapshot(
                    session,
                    members=members,
                    datasets=_pins(members),
                    now=_NOW,
                    from_version=7,
                    desired_version=8,
                )
                rollout.rescore_cohort_target = 99
                await session.flush()

    async def test_out_of_range_target_fails_loudly_in_code_too(
        self, session: AsyncSession
    ) -> None:
        """Defense in depth behind the CHECK constraint.

        ``set_committed_value`` writes the attribute without marking the row
        dirty, so this reproduces a row that reached the process out of range
        (a hand edit, or a constraint dropped in a future migration) rather
        than testing the constraint again.
        """
        async with session.begin():
            agent_ids = await _seed_era(session, version=7, count=6, offset=7000)
            members = _members(agent_ids)
            rollout = await create_rollout_snapshot(
                session,
                members=members,
                datasets=_pins(members),
                now=_NOW,
                from_version=7,
                desired_version=8,
            )
            await session.flush()
            set_committed_value(rollout, "rescore_cohort_target", 99)
            with pytest.raises(RuntimeError, match="outside"):
                await _rollout_rescore_cohort(session, rollout=rollout)
