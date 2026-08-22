"""Real-Postgres tests for shadow core qualification hysteresis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.db.models import Agent
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    observe_core_qualification,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_BENCH_VERSION = 12
_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _policy(*, bench_version: int = _BENCH_VERSION) -> CoreQualificationPolicy:
    return CoreQualificationPolicy(
        schema="ditto-core-qualification-policy-v1",
        weight_eligible=False,
        bench_version=bench_version,
        enter_composite=0.8,
        enter_tool_mean=0.8,
        enter_memory_mean=0.8,
        exit_composite=0.7,
        exit_tool_mean=0.7,
        exit_memory_mean=0.7,
        enter_observations=2,
        exit_observations=2,
    )


async def _seed_agent(session: AsyncSession) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5CoreQualificationMiner1111111111111111111111111",
        name="core-qualification-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        screened_image_sha256="cd" * 32,
        screened_image_size_bytes=1234,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref="ditto-screen/core-qualification:latest",
        screened_image_upload_id=uuid4(),
        screened_image_verified_at=_NOW,
        created_at=_NOW,
    )
    async with session.begin():
        session.add(agent)
    return agent


async def _scores(
    session: AsyncSession,
    *,
    agent_id,
    wave: int,
    composite: float,
    n: int = MIN_ELIGIBLE_CASES,
    indices: tuple[int, ...] = (0, 1, 2),
) -> None:
    async with session.begin():
        for index in indices:
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=f"validator-{index}",
                bench_version=_BENCH_VERSION,
                run_id=f"run-{wave}-{index}",
                seed=wave * 10 + index,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=100,
                n=n,
                generated_at=_NOW + timedelta(minutes=wave),
                signature=(f"{index + 1:02x}" * 64),
            )


async def _observe(session: AsyncSession, agent_id, wave: int):
    async with session.begin():
        result = await observe_core_qualification(
            session,
            agent_id=agent_id,
            bench_version=_BENCH_VERSION,
            now=_NOW + timedelta(minutes=wave, seconds=30),
        )
    assert result is not None
    return result


async def test_core_qualification_requires_policy_and_quorum(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        assert (
            await observe_core_qualification(
                session,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
                now=_NOW,
            )
            is None
        )
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=_policy(),
            reason="start shadow qualification",
            actor="test-admin",
        )
    async with session.begin():
        assert (
            await observe_core_qualification(
                session,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
                now=_NOW,
            )
            is None
        )


async def test_core_qualification_enters_and_exits_with_hysteresis(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=_policy(),
            reason="calibrated shadow thresholds",
            actor="test-admin",
        )

    await _scores(session, agent_id=agent.agent_id, wave=1, composite=0.85)
    first = await _observe(session, agent.agent_id, 1)
    assert first.row.decision == "pending_entry"
    assert first.row.enter_streak == 1
    assert first.row.qualified is False

    replay = await _observe(session, agent.agent_id, 1)
    assert replay.idempotent is True
    assert replay.row.observation_id == first.row.observation_id

    await _scores(
        session,
        agent_id=agent.agent_id,
        wave=2,
        composite=0.85,
        indices=(0,),
    )
    partial = await _observe(session, agent.agent_id, 2)
    assert partial.row.decision == "partial_wave"
    assert partial.row.complete_wave is False
    assert partial.row.enter_streak == 1
    assert partial.row.qualified is False

    await _scores(
        session,
        agent_id=agent.agent_id,
        wave=2,
        composite=0.85,
        indices=(1,),
    )
    assert (await _observe(session, agent.agent_id, 2)).row.decision == "partial_wave"
    await _scores(
        session,
        agent_id=agent.agent_id,
        wave=2,
        composite=0.85,
        indices=(2,),
    )
    entered = await _observe(session, agent.agent_id, 2)
    assert entered.row.decision == "entered"
    assert entered.row.complete_wave is True
    assert entered.row.qualified is True
    assert entered.row.sequence > partial.row.sequence

    await _scores(session, agent_id=agent.agent_id, wave=3, composite=0.65)
    pending_exit = await _observe(session, agent.agent_id, 3)
    assert pending_exit.row.decision == "pending_exit"
    assert pending_exit.row.exit_streak == 1
    assert pending_exit.row.qualified is True

    await _scores(session, agent_id=agent.agent_id, wave=4, composite=0.65)
    exited = await _observe(session, agent.agent_id, 4)
    assert exited.row.decision == "exited"
    assert exited.row.exit_streak == 2
    assert exited.row.qualified is False


async def test_core_qualification_resets_on_artifact_and_rejects_short_runs(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=_policy(),
            reason="calibrated shadow thresholds",
            actor="test-admin",
        )
    await _scores(session, agent_id=agent.agent_id, wave=1, composite=0.9)
    assert (await _observe(session, agent.agent_id, 1)).row.enter_streak == 1

    async with session.begin():
        stored = await session.get(Agent, agent.agent_id, with_for_update=True)
        assert stored is not None
        stored.sha256 = "bc" * 32
    await _scores(
        session,
        agent_id=agent.agent_id,
        wave=2,
        composite=0.9,
        n=MIN_ELIGIBLE_CASES - 1,
    )
    reset = await _observe(session, agent.agent_id, 2)
    assert reset.row.artifact_sha256 == "bc" * 32
    assert reset.row.full_size is False
    assert reset.row.entry_passed is False
    assert reset.row.enter_streak == 0
    assert reset.row.decision == "below_entry"


async def test_database_binds_observation_to_exact_policy_checksum(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=_policy(),
            reason="calibrated shadow thresholds",
            actor="test-admin",
        )
    await _scores(session, agent_id=agent.agent_id, wave=1, composite=0.9)
    observed = await _observe(session, agent.agent_id, 1)

    with pytest.raises(SAIntegrityError):
        async with session.begin():
            observed.row.policy_checksum = "ff" * 32
            await session.flush()


async def test_observation_does_not_bake_current_quorum_size_into_schema(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=_policy(),
            reason="exercise future validator fleet growth",
            actor="test-admin",
        )
    await _scores(
        session,
        agent_id=agent.agent_id,
        wave=1,
        composite=0.9,
        indices=tuple(range(9)),
    )

    observed = await _observe(session, agent.agent_id, 1)

    assert observed.row.score_count == 9
    assert len(observed.row.score_evidence["scores"]) == 9
