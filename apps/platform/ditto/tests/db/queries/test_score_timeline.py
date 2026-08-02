from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, Score
from ditto.db.queries.scores import list_memory_leader_timeline

# Two eras, because the timeline is a per-version running high and the first
# test proves the high does not carry across a version boundary. Which two is
# arbitrary -- they used to be 2 and 3 -- but ``scores_bench_version_floor``
# refuses anything under MIN_SCOREABLE_BENCH_VERSION, so they start at the live
# era and count up.
_BENCH_VERSION = 7
_NEXT_BENCH_VERSION = 8


async def _add_scored_agent(
    session: AsyncSession,
    *,
    bench_version: int,
    memory_scores: list[float],
    at: datetime,
    name: str,
    status: AgentStatus = AgentStatus.SCORED,
    n: int = 114,
) -> None:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="5" + name[0].upper() * 47,
            name=name,
            sha256=name[0] * 64,
            size_bytes=1024,
            status=status,
            created_at=at - timedelta(hours=1),
        )
    )
    await session.flush()
    for index, memory_mean in enumerate(memory_scores):
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=f"validator-{index}",
                bench_version=bench_version,
                run_id=f"{name}-{index}",
                signature="ab" * 64,
                seed=42,
                composite=max(memory_mean - 0.05, 0.01),
                tool_mean=0.8,
                memory_mean=memory_mean,
                median_ms=500,
                n=n,
                generated_at=at + timedelta(minutes=index),
                created_at=at + timedelta(minutes=index),
                updated_at=at + timedelta(minutes=index),
            )
        )


async def test_memory_timeline_keeps_only_finalized_running_highs(
    session: AsyncSession,
) -> None:
    base = datetime(2026, 7, 8, tzinfo=UTC)
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.3, 0.4, 0.5],
        at=base,
        name="alpha",
    )
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.6, 0.7, 0.8],
        at=base + timedelta(days=1),
        name="bravo",
    )
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.5, 0.6, 0.7],
        at=base + timedelta(days=2),
        name="charlie",
    )
    await _add_scored_agent(
        session,
        bench_version=_NEXT_BENCH_VERSION,
        memory_scores=[0.4, 0.5, 0.6],
        at=base + timedelta(days=11),
        name="delta",
    )
    await _add_scored_agent(
        session,
        bench_version=_NEXT_BENCH_VERSION,
        memory_scores=[0.98, 0.99],
        at=base + timedelta(days=12),
        name="echo",
    )
    await _add_scored_agent(
        session,
        bench_version=_NEXT_BENCH_VERSION,
        memory_scores=[0.98, 0.99, 1.0],
        at=base + timedelta(days=13),
        name="foxtrot",
        status=AgentStatus.REJECTED,
    )
    await session.commit()

    points = await list_memory_leader_timeline(
        session, bench_versions=[_BENCH_VERSION, _NEXT_BENCH_VERSION]
    )

    assert [(point.bench_version, point.agent_name) for point in points] == [
        (_BENCH_VERSION, "alpha"),
        (_BENCH_VERSION, "bravo"),
        (_NEXT_BENCH_VERSION, "delta"),
    ]
    assert [point.memory_mean for point in points] == [0.4, 0.7, 0.5]
    assert all(point.score_count == 3 for point in points)


async def test_memory_timeline_places_replaced_scores_at_acceptance_time(
    session: AsyncSession,
) -> None:
    base = datetime(2026, 7, 8, tzinfo=UTC)
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.3, 0.4, 0.5],
        at=base,
        name="alpha",
    )
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.5, 0.6, 0.7],
        at=base + timedelta(days=1),
        name="bravo",
    )
    await session.flush()
    alpha_scores = list(
        await session.scalars(select(Score).join(Agent).where(Agent.name == "alpha"))
    )
    for score, memory_mean in zip(alpha_scores, [0.8, 0.9, 1.0], strict=True):
        score.memory_mean = memory_mean
        score.composite = memory_mean - 0.05
        score.updated_at = base + timedelta(days=2)
    await session.commit()

    points = await list_memory_leader_timeline(session, bench_versions=[_BENCH_VERSION])

    assert [point.agent_name for point in points] == ["bravo", "alpha"]
    assert [point.recorded_at for point in points] == [
        base + timedelta(days=1, minutes=2),
        base + timedelta(days=2),
    ]


async def test_memory_timeline_reduces_running_highs_after_release_cutoff(
    session: AsyncSession,
) -> None:
    base = datetime(2026, 7, 8, tzinfo=UTC)
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.8, 0.9, 1.0],
        at=base,
        name="alpha",
    )
    await _add_scored_agent(
        session,
        bench_version=_BENCH_VERSION,
        memory_scores=[0.5, 0.6, 0.7],
        at=base + timedelta(days=2),
        name="bravo",
    )
    await session.commit()

    points = await list_memory_leader_timeline(
        session,
        bench_versions=[_BENCH_VERSION],
        not_before_by_version={_BENCH_VERSION: base + timedelta(days=1)},
    )

    assert [point.agent_name for point in points] == ["bravo"]
    assert [point.memory_mean for point in points] == [0.6]
