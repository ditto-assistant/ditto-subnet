"""The operator flip rule on the benchmark-rollout authority switch.

Without the rule the ledger flips to the desired bench version only when the
frozen priority cohort holds a 3/3 quorum AND five ranked owner families do. The
rule (``FlipRuleSettings``, frozen onto the rollout at start) lets a thin board
flip: the same condition with a lower family count, or -- once the deadline has
passed -- a lower floor with no wait for the priority cohort.

Every positive case below has a twin with the rule off that must NOT flip on the
identical state; that twin is the negative control that makes the positive
evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.public import PublicBenchRolloutResponse
from ditto.db.models import BenchmarkRollout, BenchmarkRolloutAudit, Score
from ditto.db.queries.benchmark_rollout import (
    DESIRED_AUTHORITY_EARNED_EVENT,
    FLIP_PATH_DEADLINE,
    FLIP_PATH_PRIORITY_QUORUM,
    MIN_DESIRED_AUTHORITY_AGENTS,
    RolloutSnapshotMember,
    active_bench_version,
    bench_promotion_requirement,
    create_rollout_snapshot,
    desired_authority_earned,
    flip_deadline_at,
    maybe_activate_rollout,
    maybe_record_desired_authority,
    rollout_state,
)
from ditto.tests.db.queries.test_benchmark_rollout import (
    _activation_requirements,
    _desired_quorum,
    _seed_non_member_ranked_agent,
    _seeded_rollout_session,
)

pytestmark = pytest.mark.asyncio


def _enable_rule(
    rollout: BenchmarkRollout,
    *,
    min_agents: int = 5,
    deadline_hours: int = 36,
    at_deadline: int = 3,
) -> None:
    rollout.flip_rule_enabled = True
    rollout.flip_min_agents = min_agents
    rollout.flip_deadline_hours = deadline_hours
    rollout.flip_min_agents_at_deadline = at_deadline


async def _seed_outsiders(
    session: AsyncSession, *, now: datetime, desired: int, count: int
) -> list[UUID]:
    return [
        await _seed_non_member_ranked_agent(
            session, now=now, desired_version=desired, index=index
        )
        for index in range(count)
    ]


async def _earned_events(
    session: AsyncSession, rollout: BenchmarkRollout
) -> list[BenchmarkRolloutAudit]:
    return list(
        await session.scalars(
            select(BenchmarkRolloutAudit).where(
                BenchmarkRolloutAudit.rollout_id == rollout.rollout_id,
                BenchmarkRolloutAudit.event == DESIRED_AUTHORITY_EARNED_EVENT,
            )
        )
    )


async def test_deadline_flips_three_families_only_with_the_rule_on(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The negative control: 3 ranked families, deadline passed, cohort empty."""
    now = datetime.now(UTC).replace(microsecond=0)
    after_deadline = now + timedelta(hours=37)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        source = await active_bench_version(session)
        await _seed_outsiders(session, now=now, desired=desired, count=3)

        # Rule OFF (every rollout that predates the rule, and the shipped
        # default): three ranked families and an incomplete priority cohort
        # never flip, however long we wait.
        assert rollout.flip_rule_enabled is False
        assert await maybe_record_desired_authority(session, rollout, now=after_deadline) is False
        assert await active_bench_version(session, open_transition=rollout) == source
        assert await _earned_events(session, rollout) == []

        # Rule ON over the identical state: the first write past the deadline
        # seals the flip durably.
        _enable_rule(rollout)
        await session.flush()
        assert await maybe_record_desired_authority(session, rollout, now=after_deadline) is True
        assert await desired_authority_earned(session, rollout)
        assert await active_bench_version(session, open_transition=rollout) == desired
        [event] = await _earned_events(session, rollout)
        assert event.payload["flip_path"] == FLIP_PATH_DEADLINE
        assert event.payload["ranked_quorum_agents"] == 3
        assert event.payload["flip_min_agents_at_deadline"] == 3
        assert event.payload["flip_deadline_hours"] == 36


async def test_deadline_branch_waits_for_the_deadline_and_the_floor(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        source = await active_bench_version(session)
        _enable_rule(rollout)
        await _seed_outsiders(session, now=now, desired=desired, count=3)
        deadline = flip_deadline_at(rollout)
        assert deadline == now + timedelta(hours=36)

        # One second early: three families, no flip.
        assert not await maybe_record_desired_authority(
            session, rollout, now=deadline - timedelta(seconds=1)
        )
        assert await active_bench_version(session, open_transition=rollout) == source

        # A reader that arrives after the deadline but before any write still
        # sees the old era: the read path is deliberately clock-free, so two
        # readers can never disagree for the second around the deadline.
        assert await active_bench_version(session, open_transition=rollout) == source


async def test_deadline_branch_needs_the_floor_however_late(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        # Two families is below the floor even long after the deadline.
        _enable_rule(rollout)
        await _seed_outsiders(
            session, now=now, desired=rollout.desired_version, count=2
        )
        assert not await maybe_record_desired_authority(
            session, rollout, now=now + timedelta(hours=500)
        )


async def test_flip_is_sealed_by_the_poll_path_and_closes_the_rollout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """``refresh_rolling_qualification`` reaches the flip through this call."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        _enable_rule(rollout)
        await _seed_outsiders(
            session, now=now, desired=rollout.desired_version, count=3
        )
        late = now + timedelta(hours=40)
        activated = await maybe_activate_rollout(
            session,
            rollout,
            now=late,
            inference_requirements=_activation_requirements(),
        )
        assert activated is True
        assert rollout.status == "activated"
        assert rollout.activated_at == late

        # Sticky: the quorum rows can lapse afterwards and nothing rolls back.
        await session.execute(
            Score.__table__.delete().where(
                Score.bench_version == rollout.desired_version
            )
        )
        assert await desired_authority_earned(session, rollout)


async def test_immediate_threshold_relaxes_the_family_count_not_the_cohort(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        source = await active_bench_version(session)
        # The whole priority cohort reaches quorum, but one member scores 0.0
        # and is not a ranked family: four ranked families, one short of five.
        for index, agent_id in enumerate(agent_ids):
            rows = _desired_quorum(
                agent_id, desired_version=desired, now=now, tag=str(index)
            )
            if index == 0:
                for row in rows:
                    row.composite = 0.0
            session.add_all(rows)
        await session.flush()

        # Rule off: 4 < 5, no flip.
        assert not await maybe_record_desired_authority(session, rollout, now=now)
        assert await active_bench_version(session, open_transition=rollout) == source

        # Rule on with an immediate threshold of 4: flips at once, before any
        # deadline, because the cohort is complete.
        _enable_rule(rollout, min_agents=4, at_deadline=3)
        await session.flush()
        assert await maybe_record_desired_authority(session, rollout, now=now)
        assert await active_bench_version(session, open_transition=rollout) == desired
        [event] = await _earned_events(session, rollout)
        assert event.payload["flip_path"] == FLIP_PATH_PRIORITY_QUORUM
        assert event.payload["ranked_quorum_agents"] == 4


async def test_immediate_threshold_still_waits_for_the_priority_cohort(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Three ranked outsiders must not flip before the deadline, even at N=3."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        source = await active_bench_version(session)
        _enable_rule(rollout, min_agents=3, at_deadline=3)
        await _seed_outsiders(
            session, now=now, desired=rollout.desired_version, count=3
        )
        assert not await maybe_record_desired_authority(session, rollout, now=now)
        assert await active_bench_version(session, open_transition=rollout) == source


async def test_enabled_rule_at_defaults_is_the_original_rule_before_the_deadline(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Enabling the rule changes nothing until its deadline is reached."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        source = await active_bench_version(session)
        _enable_rule(rollout)
        for index in (0, 1, 2, 3):
            session.add_all(
                _desired_quorum(
                    agent_ids[index], desired_version=desired, now=now, tag=str(index)
                )
            )
        await session.flush()
        # Four ranked families, the fifth cohort member missing: held.
        assert not await maybe_record_desired_authority(session, rollout, now=now)
        assert await active_bench_version(session, open_transition=rollout) == source
        # The fifth arrives: the original rule flips, on the original path.
        session.add_all(
            _desired_quorum(
                agent_ids[4], desired_version=desired, now=now, tag="4"
            )
        )
        await session.flush()
        assert await maybe_record_desired_authority(session, rollout, now=now)
        [event] = await _earned_events(session, rollout)
        assert event.payload["flip_path"] == FLIP_PATH_PRIORITY_QUORUM
        assert event.payload["ranked_quorum_agents"] == MIN_DESIRED_AUTHORITY_AGENTS


async def test_disabled_rule_writes_the_original_audit_payload(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        for index, agent_id in enumerate(agent_ids):
            session.add_all(
                _desired_quorum(
                    agent_id, desired_version=desired, now=now, tag=str(index)
                )
            )
        await session.flush()
        assert await maybe_record_desired_authority(session, rollout, now=now)
        [event] = await _earned_events(session, rollout)
        assert event.payload == {
            "bench_version": desired,
            "ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
            "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
        }


async def test_rule_is_frozen_at_start_and_bounds_are_enforced(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        # Defaults describe a DISABLED rule.
        assert rollout.flip_rule_enabled is False
        assert (
            rollout.flip_min_agents,
            rollout.flip_deadline_hours,
            rollout.flip_min_agents_at_deadline,
        ) == (5, 36, 3)
        assert flip_deadline_at(rollout) is None

        members = [
            RolloutSnapshotMember(uuid_, f"hotkey-{i}", 0.5)
            for i, uuid_ in enumerate(_agent_ids)
        ]
        # create_rollout_snapshot refuses out-of-range freezes before touching
        # the database (an internal caller bypassing the wire model).
        for kwargs in (
            {"flip_min_agents": 2},
            {"flip_min_agents": 6},
            {"flip_min_agents_at_deadline": 2},
            {"flip_min_agents": 4, "flip_min_agents_at_deadline": 5},
            {"flip_deadline_hours": 0},
            {"flip_deadline_hours": 169},
        ):
            with pytest.raises(ValueError):
                await create_rollout_snapshot(
                    session,
                    members=members,
                    datasets={},
                    now=now,
                    from_version=2,
                    desired_version=rollout.desired_version + 1,
                    flip_rule_enabled=True,
                    **kwargs,  # type: ignore[arg-type]
                )


async def test_database_refuses_out_of_bound_rules(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An application bug cannot write beneath the bound: one savepoint per value."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (session, _ids, rollout):
        for assignment in (
            "flip_min_agents = 2",
            "flip_min_agents = 6",
            "flip_deadline_hours = 0",
            "flip_deadline_hours = 169",
            "flip_min_agents_at_deadline = 2",
            # The deadline floor may not exceed the immediate threshold.
            "flip_min_agents = 3, flip_min_agents_at_deadline = 4",
        ):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            f"UPDATE benchmark_rollouts SET {assignment} "
                            "WHERE rollout_id = :rollout_id"
                        ),
                        {"rollout_id": rollout.rollout_id},
                    )
        # A legal value goes through, so the loop above proves the bound and
        # not a broken statement.
        await session.execute(
            text(
                "UPDATE benchmark_rollouts SET flip_min_agents = 4, "
                "flip_min_agents_at_deadline = 4 WHERE rollout_id = :rollout_id"
            ),
            {"rollout_id": rollout.rollout_id},
        )


async def test_telemetry_and_public_requirement_describe_the_rule(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    async with _seeded_rollout_session(session_maker, now) as (
        session,
        _agent_ids,
        rollout,
    ):
        desired = rollout.desired_version
        source = await active_bench_version(session)

        off = await rollout_state(session, now=now)
        assert off["flip_rule"]["enabled"] is False
        assert off["flip_rule"]["deadline_at"] is None
        assert off["flip_rule"]["expired_quorum_agents"] is None
        # Disabled wording is the original sentence, character for character.
        assert off["promotion_requirement"] == bench_promotion_requirement(
            emission_version=source,
            rollout_version=desired,
            priority_cohort_size=5,
        )

        _enable_rule(rollout, min_agents=4, deadline_hours=24, at_deadline=3)
        await _seed_outsiders(session, now=now, desired=desired, count=3)
        before = await rollout_state(session, now=now + timedelta(hours=1))
        rule = before["flip_rule"]
        assert rule["enabled"] is True
        assert rule["min_agents_to_flip"] == 4
        assert rule["flip_deadline_hours"] == 24
        assert rule["min_agents_at_deadline"] == 3
        assert rule["deadline_at"] == (now + timedelta(hours=24)).isoformat()
        assert rule["deadline_elapsed"] is False
        assert rule["ranked_quorum_agents"] == 3
        assert rule["immediate_ready"] is False
        assert rule["deadline_ready"] is False
        assert "at least 4 agents hold" in before["promotion_requirement"]
        assert "24 hours after the rollout started" in before["promotion_requirement"]
        PublicBenchRolloutResponse.model_validate(before)

        after = await rollout_state(session, now=now + timedelta(hours=25))
        assert after["flip_rule"]["deadline_elapsed"] is True
        assert after["flip_rule"]["deadline_ready"] is True
        assert after["flip_rule"]["authority_earned"] is False
