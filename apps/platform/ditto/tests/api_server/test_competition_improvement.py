"""Competition improvement metrics: pure arithmetic, the job and the operator read."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.competition_improvement import (
    ChampionPin,
    ForkCandidate,
    PolicyRevision,
    ScoredAgent,
    SourceRelease,
    build_daily,
    classify_forks,
    embargo_windows,
    find_new_highs,
    noise_floor,
)
from ditto.api_server.competition_improvement_job import CompetitionImprovementJob
from ditto.api_server.dependencies import get_session
from ditto.api_server.distinctive_content import ITEMS_VERSION
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    AgentStatus,
    ArtifactFetchAudit,
    ArtifactReleaseSettingsRevision,
    CompetitionImprovementDaily,
    CompetitionReleaseFork,
    Score,
)
from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT

_T0 = datetime(2026, 10, 20, 12, tzinfo=UTC)
_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_DEFAULT = PolicyRevision(0, 48, "public", None)


def _scored(
    owner: str, hours: float, composite: float, *, uploaded: float | None = None
) -> ScoredAgent:
    return ScoredAgent(
        agent_id=uuid4(),
        owner=owner,
        name=owner,
        version=1,
        bench_version=14,
        composite=composite,
        finalized_at=_T0 + timedelta(hours=hours),
        uploaded_at=_T0
        + timedelta(hours=uploaded if uploaded is not None else hours - 1),
        status="scored",
    )


def _release(agent: ScoredAgent, hours: float, embargo: int = 48) -> SourceRelease:
    return SourceRelease(
        agent_id=agent.agent_id,
        owner=agent.owner,
        name=agent.name,
        version=agent.version,
        bench_version=14,
        released_at=_T0 + timedelta(hours=hours),
        uploaded_at=agent.uploaded_at,
        embargo_hours=embargo,
        disclosure="public",
        base_composite=agent.composite,
    )


def _candidate(
    base: ScoredAgent,
    fork: ScoredAgent,
    containment: float | None,
    novel: float | None,
    *,
    declared: bool = False,
) -> ForkCandidate:
    return ForkCandidate(
        base_agent_id=base.agent_id,
        fork_agent_id=fork.agent_id,
        fork_owner=fork.owner,
        fork_name=fork.name,
        fork_version=fork.version,
        fork_status=fork.status,
        fork_uploaded_at=fork.uploaded_at,
        containment=containment,
        novel_fraction=novel,
        declared=declared,
    )


def test_forks_need_containment_or_declaration_and_another_owner() -> None:
    base = _scored("a", 0, 0.90)
    thin = _scored("b", 60, 0.91)
    reworked = _scored("c", 70, 0.93)
    unrelated = _scored("d", 80, 0.95)
    declared = _scored("e", 90, 0.89)
    same_owner = _scored("a", 75, 0.94)
    early = _scored("f", 30, 0.92)
    release = _release(base, 48)
    seeds = {
        (base.agent_id, 14): {1: 0.90, 2: 0.88},
        (thin.agent_id, 14): {1: 0.92, 2: 0.90, 3: 0.5},
    }
    rows = classify_forks(
        [release],
        [
            _candidate(base, thin, 0.98, 0.02),
            _candidate(base, reworked, 0.6, 0.4),
            _candidate(base, unrelated, 0.1, 0.9),
            _candidate(base, declared, None, None, declared=True),
            _candidate(base, same_owner, 0.99, 0.01),
            _candidate(base, early, 0.99, 0.01),
        ],
        composites={
            (a.agent_id, 14): a.composite
            for a in (base, thin, reworked, unrelated, declared)
        },
        seed_scores=seeds,
    )
    by_fork = {row.fork_agent_id: row for row in rows}
    assert set(by_fork) == {thin.agent_id, reworked.agent_id, declared.agent_id}
    assert by_fork[thin.agent_id].thin is True
    assert by_fork[reworked.agent_id].thin is False
    assert by_fork[thin.agent_id].lag_hours == 11.0
    assert by_fork[thin.agent_id].delta == pytest.approx(0.01)
    assert by_fork[thin.agent_id].shared_seed_count == 2
    assert by_fork[thin.agent_id].shared_seed_delta == pytest.approx(0.02)
    assert by_fork[declared.agent_id].declared is True
    assert by_fork[declared.agent_id].delta == pytest.approx(-0.01)


def test_a_fork_is_attributed_to_one_base_of_a_release_line() -> None:
    older = _scored("a", 0, 0.90)
    newer = _scored("a", 30, 0.92)
    fork = _scored("b", 100, 0.93)
    declared_fork = _scored("c", 110, 0.91)
    releases = [_release(older, 48), _release(newer, 72)]
    rows = classify_forks(
        releases,
        [
            _candidate(older, fork, 0.903, 0.2),
            _candidate(newer, fork, 0.898, 0.05),
            _candidate(older, declared_fork, None, None, declared=True),
            _candidate(newer, declared_fork, 0.95, 0.01),
        ],
        composites={},
        seed_scores={},
    )
    by_fork = {row.fork_agent_id: row for row in rows}
    assert len(rows) == 2
    # Near-equal containment goes to the latest release, which sets the lag.
    assert by_fork[fork.agent_id].base_agent_id == newer.agent_id
    assert by_fork[fork.agent_id].lag_hours == 27.0
    assert by_fork[declared_fork.agent_id].base_agent_id == older.agent_id


def test_new_highs_classify_the_driver_and_gap() -> None:
    base = _scored("a", 0, 0.90, uploaded=-1)
    lower = _scored("x", 5, 0.85)
    iteration = _scored("a", 24, 0.92)
    original = _scored("n", 48, 0.93)
    fork = _scored("b", 96, 0.95)
    forks = classify_forks(
        [_release(base, 60)],
        [_candidate(base, fork, 0.9, 0.05)],
        composites={(base.agent_id, 14): 0.9, (fork.agent_id, 14): 0.95},
        seed_scores={},
    )
    highs = find_new_highs(
        [base, lower, iteration, original, fork],
        forks=forks,
        owner_first_upload={
            "a": base.uploaded_at,
            "x": lower.uploaded_at,
            "n": original.uploaded_at,
            "b": fork.uploaded_at,
        },
    )
    assert [(h.agent_id, h.driver) for h in highs] == [
        (iteration.agent_id, "same_owner_iteration"),
        (original.agent_id, "original"),
        (fork.agent_id, "released_fork"),
    ]
    assert highs[0].delta == pytest.approx(0.02)
    assert highs[0].days_since_previous_high == 1.0
    assert highs[2].base_agent_id == base.agent_id
    assert highs[2].days_since_previous_high == 2.0


def test_daily_rows_track_frontier_owner_top_and_champion() -> None:
    first = _scored("a", 1, 0.90)
    same_owner = _scored("a", 2, 0.91)
    other = _scored("b", 30, 0.89)
    pins = [
        ChampionPin(14, _T0 + timedelta(hours=3), first.agent_id),
        ChampionPin(14, _T0 + timedelta(hours=31), other.agent_id),
        ChampionPin(13, _T0 + timedelta(hours=32), uuid4()),
    ]
    revision = PolicyRevision(1, 24, "public", _T0 + timedelta(hours=20))
    rows = build_daily(
        14,
        [first, same_owner, other],
        first_day=_T0.date(),
        last_day=_T0.date() + timedelta(days=2),
        champions=pins,
        releases=[],
        forks=[],
        new_highs=[],
        revisions=[revision],
        default_policy=_DEFAULT,
        top_n=5,
    )
    assert [r.frontier_composite for r in rows] == [0.91, 0.91, 0.91]
    assert [len(r.top) for r in rows] == [1, 2, 2]
    assert rows[1].top_n_min == 0.89
    assert rows[0].champion_agent_id == first.agent_id
    assert rows[1].champion_agent_id == other.agent_id
    assert rows[1].champion_composite == 0.89
    assert [r.embargo_hours for r in rows] == [48, 24, 24]


def test_embargo_windows_group_releases_by_the_policy_in_force() -> None:
    base_a = _scored("a", 0, 0.90)
    base_b = _scored("b", 0, 0.91)
    fork = _scored("c", 100, 0.93)
    revisions = [
        PolicyRevision(1, 24, "public", _T0),
        PolicyRevision(2, 48, "public", _T0 + timedelta(days=3)),
    ]
    releases = [_release(base_a, 24, 24), _release(base_b, 24 * 4)]
    forks = classify_forks(
        releases,
        [_candidate(base_a, fork, 0.9, 0.05)],
        composites={(base_a.agent_id, 14): 0.9, (fork.agent_id, 14): 0.93},
        seed_scores={},
    )
    windows = embargo_windows(
        revisions,
        default_policy=_DEFAULT,
        releases=releases,
        forks=forks,
        highs=[],
        daily=[],
        now=_T0 + timedelta(days=6),
    )
    assert [(w.embargo_hours, w.releases, w.forks) for w in windows] == [
        (24, 1, 1),
        (48, 1, 0),
    ]
    assert windows[0].thin_fork_share == 1.0
    assert windows[0].days == 3.0
    assert windows[1].days == 3.0


def test_noise_floor_uses_seed_spread_of_the_leaders() -> None:
    agents = [_scored("a", 0, 0.95), _scored("b", 0, 0.94), _scored("c", 0, 0.5)]
    seeds = {
        (agents[0].agent_id, 14): {1: 0.94, 2: 0.96, 3: 0.95},
        (agents[1].agent_id, 14): {1: 0.92, 2: 0.96, 3: 0.94},
        (agents[2].agent_id, 14): {1: 0.5},
    }
    noise = noise_floor(
        14,
        agents,
        seed_scores=seeds,
        quorum={(agents[0].agent_id, 14): [0.94, 0.95, 0.96]},
        top=3,
    )
    assert noise.agents_measured == 2
    assert noise.median_seed_sd == pytest.approx(0.015)
    assert noise.single_seed_delta_sd == pytest.approx(0.015 * 2**0.5, abs=1e-6)
    assert noise.median_quorum_sem == pytest.approx(0.005774, abs=1e-6)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _agent_row(agent_id: UUID, hotkey: str, hours: float) -> Agent:
    return Agent(
        agent_id=agent_id,
        miner_hotkey=hotkey,
        name=hotkey.lower(),
        version=1,
        sha256=agent_id.hex * 2,
        status=AgentStatus.SCORED,
        created_at=_T0 + timedelta(hours=hours),
    )


def _scores(agent_id: UUID, hours: float, composites: list[float]) -> list[Score]:
    return [
        Score(
            agent_id=agent_id,
            validator_hotkey=f"validator-{index}",
            bench_version=14,
            run_id=f"{agent_id}-{index}",
            signature="ab" * 64,
            seed=7,
            composite=composite,
            tool_mean=composite,
            memory_mean=composite,
            median_ms=500,
            n=350,
            generated_at=_T0 + timedelta(hours=hours, minutes=index),
            created_at=_T0 + timedelta(hours=hours, minutes=index),
            updated_at=_T0 + timedelta(hours=hours, minutes=index),
        )
        for index, composite in enumerate(composites)
    ]


async def test_job_backfills_freezes_closed_days_and_serves_the_read(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    base_id, fork_id, other_id = uuid4(), uuid4(), uuid4()
    shared = list(range(8, 8 + 100 * 8, 8))
    async with session_maker() as session, session.begin():
        # The migration chain seeds one revision; stage ours as a child of it.
        parent = await session.scalar(
            select(func.coalesce(func.max(ArtifactReleaseSettingsRevision.revision), 0))
        )
        session.add_all(
            [
                _agent_row(base_id, "5Base", 0),
                _agent_row(fork_id, "5Fork", 60),
                _agent_row(other_id, "5Other", 20),
            ]
        )
        await session.flush()
        session.add_all(
            [
                *_scores(base_id, 1, [0.90, 0.91, 0.92]),
                *_scores(other_id, 21, [0.85, 0.86, 0.87]),
                *_scores(fork_id, 61, [0.93, 0.94, 0.95]),
                ArtifactReleaseSettingsRevision(
                    parent_revision=parent,
                    disclosure="public",
                    embargo_hours=24,
                    reason="stage a shorter window",
                    actor="operator@example.com",
                    created_at=_T0 - timedelta(days=1),
                ),
                ArtifactFetchAudit(
                    agent_id=base_id,
                    endpoint=ENDPOINT_PUBLIC_ARTIFACT,
                    requester_kind="public",
                    artifact_sha256=base_id.hex * 2,
                    fetched_at=_T0 + timedelta(hours=48),
                ),
                AgentDistinctiveItems(
                    agent_id=base_id,
                    items_version=ITEMS_VERSION,
                    item_keys=shared,
                    item_count=len(shared),
                ),
                AgentDistinctiveItems(
                    agent_id=fork_id,
                    items_version=ITEMS_VERSION,
                    item_keys=[*shared, 9001, 9002],
                    item_count=len(shared) + 2,
                ),
                AgentDistinctiveItems(
                    agent_id=other_id,
                    items_version=ITEMS_VERSION,
                    item_keys=[1, 2, 3],
                    item_count=3,
                ),
            ]
        )
    job = CompetitionImprovementJob(session_maker=session_maker)
    now = _T0 + timedelta(days=5)
    counters = await job.tick(now=now)
    assert counters["releases"] == 1
    assert counters["forks"] == 1
    assert counters["new_highs"] == 1

    async with session_maker() as session:
        days = list(
            await session.scalars(
                select(CompetitionImprovementDaily).order_by(
                    CompetitionImprovementDaily.day
                )
            )
        )
        fork = await session.get(CompetitionReleaseFork, (base_id, fork_id))
    assert [d.day for d in days] == [
        _T0.date() + timedelta(days=offset) for offset in range(6)
    ]
    assert [d.frontier_composite for d in days] == [0.91, 0.91, 0.91, 0.94, 0.94, 0.94]
    assert [d.closed for d in days] == [True, True, True, True, False, False]
    assert all(d.backfilled == d.closed for d in days)
    assert days[3].new_high_count == 1
    assert days[3].embargo_hours == 24
    assert fork is not None
    assert fork.lag_hours == 12.0
    assert fork.novel_fraction == pytest.approx(2 / 102, abs=1e-6)
    assert fork.thin is True
    assert fork.delta == pytest.approx(0.03)

    # A closed day keeps what it recorded even when a later re-score moves it.
    async with session_maker() as session, session.begin():
        row = await session.get(Score, (fork_id, 14, "validator-1"))
        assert row is not None
        row.composite = 0.99
    await job.tick(now=now)
    async with session_maker() as session:
        frozen = await session.get(
            CompetitionImprovementDaily, (14, _T0.date() + timedelta(days=3))
        )
        live = await session.get(
            CompetitionImprovementDaily, (14, _T0.date() + timedelta(days=5))
        )
    assert frozen is not None and frozen.frontier_composite == 0.94
    assert live is not None and live.frontier_composite == 0.95

    response = await client.get(
        "/api/v1/admin/competition-improvement/metrics",
        params={"bench_version": 14},
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics_version"] == "cim1"
    bench = body["benches"][0]
    assert bench["summary"]["drivers"] == {
        "original": 0,
        "same_owner_iteration": 0,
        "released_fork": 1,
    }
    assert bench["new_highs"][0]["base_agent_id"] == str(base_id)
    assert body["releases"][0]["fork_count"] == 1
    assert body["releases"][0]["thin_fork_count"] == 1
    assert body["releases"][0]["embargo_hours"] == 24
    (window,) = [w for w in body["embargo_windows"] if w["releases"]]
    assert window["embargo_hours"] == 24
    assert window["forks_per_release"] == 1.0
    assert window["new_highs_by_driver"]["released_fork"] == 1
    assert body["coverage"]["closed_days"] == 4

    since = await client.get(
        "/api/v1/admin/competition-improvement/metrics",
        params={"since": str(date(2026, 10, 24))},
        headers=_HEADERS,
    )
    assert since.status_code == 200
    assert [r["day"] for r in since.json()["benches"][0]["daily"]] == [
        "2026-10-24",
        "2026-10-25",
    ]
    assert since.json()["releases"] == []
