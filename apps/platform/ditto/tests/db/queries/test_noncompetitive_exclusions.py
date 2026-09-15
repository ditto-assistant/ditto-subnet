"""Team canary exclusions fail closed in every competition projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, BenchmarkRollout
from ditto.db.queries.benchmark_rollout import (
    historical_rescore_cohort,
    rolling_top_five,
)
from ditto.db.queries.noncompetitive_exclusions import (
    NoncompetitiveExclusionError,
    TeamCanaryExclusion,
    bind_team_canary,
    excluded_agent_ids,
    list_team_canary_agents,
    list_team_canary_exclusions,
    reserve_team_canary,
)
from ditto.db.queries.scores import (
    MIN_ELIGIBLE_CASES,
    list_eligible_ledger,
    list_memory_leader_timeline,
    list_provisional_ledger,
    ranked_quorum_agent_ids,
    upsert_score,
)

_CANARY = "5CanaryTestingHotkey" + "x" * 28
_OTHER = "5OtherMinerHotkey" + "y" * 31
_ORDINARY = "5OrdinaryMinerHotkey" + "z" * 28
_ARTIFACT = "c1" * 32
_DRIFTED_ARTIFACT = "d2" * 32
_IMAGE = "e3" * 32
_REBUILT_IMAGE = "f4" * 32
_VERSION = 7
_VALIDATORS = ("5Va", "5Vb", "5Vc")
_SEEN = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)
_REASON = "team canary for hosted coding certification"


@pytest.fixture(autouse=True)
async def _active_era(session: AsyncSession) -> None:
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_VERSION - 1,
                desired_version=_VERSION,
                status="activated",
                cohort_size=5,
                created_at=_SEEN - timedelta(days=2),
                activated_at=_SEEN - timedelta(days=1),
            )
        )


@dataclass(frozen=True)
class Seeded:
    agent_id: UUID
    miner_hotkey: str
    sha256: str


async def _agent(
    session: AsyncSession,
    *,
    hotkey: str,
    sha256: str,
    composite: float,
    status: AgentStatus = AgentStatus.SCORED,
    n: int = MIN_ELIGIBLE_CASES,
    image: str = _IMAGE,
    created_at: datetime = _SEEN,
    name: str = "agent",
) -> Seeded:
    await _fresh(session)
    agent_id = uuid4()
    agent = Agent(
        agent_id=agent_id,
        miner_hotkey=hotkey,
        name=name,
        sha256=sha256,
        size_bytes=1024,
        status=status,
        created_at=created_at,
        screened_image_sha256=image,
        screened_image_size_bytes=2048,
        screened_image_id="sha256:" + image,
        screened_image_ref=f"ditto-screen/{agent_id}:latest",
        screened_image_upload_id=uuid4(),
        screened_image_verified_at=created_at,
    )
    async with session.begin():
        session.add(agent)
        await session.flush()
        for index, validator in enumerate(_VALIDATORS):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=validator,
                bench_version=_VERSION,
                run_id=f"run-{agent_id}-{index}",
                seed=index,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=n,
                generated_at=created_at + timedelta(minutes=index + 1),
                signature="ab" * 64,
            )
    return Seeded(agent_id=agent_id, miner_hotkey=hotkey, sha256=sha256)


async def _fresh(session: AsyncSession) -> None:
    # Reads autobegin; end them so each write opens its own transaction.
    if session.in_transaction():
        await session.rollback()


async def _reserve(
    session: AsyncSession, *, hotkey: str = _CANARY, sha256: str = _ARTIFACT
) -> TeamCanaryExclusion:
    await _fresh(session)
    async with session.begin():
        return await reserve_team_canary(
            session,
            miner_hotkey=hotkey,
            artifact_sha256=sha256,
            reason=_REASON,
            actor="operator@example.com",
        )


async def _bind(
    session: AsyncSession,
    exclusion: TeamCanaryExclusion,
    agent: Seeded,
    *,
    hotkey: str | None = None,
    sha256: str | None = None,
    image: str = _IMAGE,
) -> TeamCanaryExclusion:
    await _fresh(session)
    async with session.begin():
        return await bind_team_canary(
            session,
            exclusion_id=exclusion.exclusion_id,
            agent_id=agent.agent_id,
            miner_hotkey=hotkey or agent.miner_hotkey,
            artifact_sha256=sha256 or agent.sha256,
            screened_image_sha256=image,
            reason="bound after screening produced the image",
            actor="operator@example.com",
        )


async def _competition_view(session: AsyncSession) -> dict[str, set[UUID]]:
    ledger = await list_eligible_ledger(session)
    timeline = await list_memory_leader_timeline(session, bench_versions=[_VERSION])
    view = {
        "eligible": {row.agent_id for row in ledger if row.eligible},
        "excluded_rows": {row.agent_id for row in ledger if row.competition_excluded},
        "ledger": {row.agent_id for row in ledger},
        "ranked_quorum": await ranked_quorum_agent_ids(session, bench_version=_VERSION),
        "top_five": {m.agent_id for m in await rolling_top_five(session)},
        "rescore_cohort": {
            m.agent_id
            for m in await historical_rescore_cohort(session, source_version=_VERSION)
        },
        "leader_timeline": {point.agent_id for point in timeline},
    }
    await _fresh(session)
    return view


async def test_default_empty_table_excludes_nothing(session: AsyncSession) -> None:
    ordinary = await _agent(session, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.5)
    canary_like = await _agent(session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9)
    assert await list_team_canary_exclusions(session) == ()
    view = await _competition_view(session)
    for projection in ("eligible", "ranked_quorum", "top_five", "rescore_cohort"):
        assert {ordinary.agent_id, canary_like.agent_id} <= view[projection]
    assert view["excluded_rows"] == set()


async def test_reserved_then_bound_canary_fails_closed_in_every_projection(
    session: AsyncSession,
) -> None:
    exclusion = await _reserve(session)
    canary = await _agent(session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.95)
    ordinary = await _agent(session, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.5)

    for bound in (False, True):
        if bound:
            exclusion = await _bind(session, exclusion, canary)
        view = await _competition_view(session)
        # Still graded and still present for copy detection and image retention.
        assert canary.agent_id in view["ledger"]
        assert view["excluded_rows"] == {canary.agent_id}
        for projection in (
            "eligible",
            "ranked_quorum",
            "top_five",
            "rescore_cohort",
            "leader_timeline",
        ):
            assert canary.agent_id not in view[projection], projection
            assert ordinary.agent_id in view[projection], projection
        ledger = await list_eligible_ledger(session)
        assert ledger[0].agent_id == ordinary.agent_id

    assert await excluded_agent_ids(session, [canary.agent_id, ordinary.agent_id]) == {
        canary.agent_id
    }
    agents = await list_team_canary_agents(session, [exclusion])
    assert [(a.agent_id, a.state) for a in agents[exclusion.exclusion_id]] == [
        (canary.agent_id, "bound")
    ]


async def test_bound_canary_stays_excluded_after_screened_image_drift(
    session: AsyncSession,
) -> None:
    exclusion = await _reserve(session)
    canary = await _agent(session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9)
    exclusion = await _bind(session, exclusion, canary)
    await _fresh(session)
    async with session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == canary.agent_id)
            .values(
                screened_image_sha256=_REBUILT_IMAGE,
                screened_image_id="sha256:" + _REBUILT_IMAGE,
            )
        )
    view = await _competition_view(session)
    assert canary.agent_id not in view["eligible"]
    assert canary.agent_id not in view["ranked_quorum"]
    agents = await list_team_canary_agents(session, [exclusion])
    assert [a.state for a in agents[exclusion.exclusion_id]] == ["binding_drift"]


async def test_artifact_drift_is_not_this_exclusion_and_cannot_be_bound(
    session: AsyncSession,
) -> None:
    exclusion = await _reserve(session)
    drifted = await _agent(
        session, hotkey=_CANARY, sha256=_DRIFTED_ARTIFACT, composite=0.9
    )
    canary = await _agent(
        session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.8, name="canary"
    )
    view = await _competition_view(session)
    # A hotkey alone never excludes; a different artifact needs its own review.
    assert drifted.agent_id in view["eligible"]
    assert canary.agent_id not in view["eligible"]
    with pytest.raises(NoncompetitiveExclusionError):
        await _bind(session, exclusion, drifted, sha256=_ARTIFACT)
    with pytest.raises(NoncompetitiveExclusionError):
        await _bind(session, exclusion, canary, image=_REBUILT_IMAGE)
    assert (await list_team_canary_exclusions(session))[0].agent_id is None


async def test_hotkey_mismatch_is_not_this_exclusion_and_cannot_be_bound(
    session: AsyncSession,
) -> None:
    exclusion = await _reserve(session)
    same_artifact_other_hotkey = await _agent(
        session, hotkey=_OTHER, sha256=_ARTIFACT, composite=0.9
    )
    view = await _competition_view(session)
    # Copy detection, not this exclusion, judges another hotkey's identical bytes.
    assert same_artifact_other_hotkey.agent_id in view["eligible"]
    assert view["excluded_rows"] == set()
    with pytest.raises(NoncompetitiveExclusionError):
        await _bind(session, exclusion, same_artifact_other_hotkey)
    with pytest.raises(NoncompetitiveExclusionError):
        await _bind(session, exclusion, same_artifact_other_hotkey, hotkey=_CANARY)


async def test_ordinary_agents_keep_identical_standing(session: AsyncSession) -> None:
    first = await _agent(session, hotkey=_ORDINARY, sha256="a1" * 32, composite=0.6)
    second = await _agent(session, hotkey=_OTHER, sha256="b2" * 32, composite=0.4)
    before = await list_eligible_ledger(session)
    await _fresh(session)
    await _reserve(session)
    after = await list_eligible_ledger(session)
    assert [(r.agent_id, r.eligible, r.composite) for r in before] == [
        (r.agent_id, r.eligible, r.composite) for r in after
    ]
    assert {first.agent_id, second.agent_id} == {r.agent_id for r in after}
    assert not any(row.competition_excluded for row in after)


@pytest.mark.parametrize(
    ("status", "n", "composite"),
    [
        (AgentStatus.BANNED, MIN_ELIGIBLE_CASES, 0.9),
        (AgentStatus.ATH_PENDING_REVIEW, MIN_ELIGIBLE_CASES, 0.9),
        (AgentStatus.REJECTED, MIN_ELIGIBLE_CASES, 0.9),
        (AgentStatus.SCORED, 20, 0.9),
        (AgentStatus.SCORED, MIN_ELIGIBLE_CASES, 0.0),
    ],
)
async def test_exclusion_never_grants_eligibility(
    session: AsyncSession, status: AgentStatus, n: int, composite: float
) -> None:
    await _reserve(session)
    canary = await _agent(
        session,
        hotkey=_CANARY,
        sha256=_ARTIFACT,
        composite=composite,
        status=status,
        n=n,
    )
    view = await _competition_view(session)
    for projection in ("eligible", "ranked_quorum", "top_five", "rescore_cohort"):
        assert canary.agent_id not in view[projection]


async def test_provisional_overlay_marks_an_evaluating_canary(
    session: AsyncSession,
) -> None:
    await _reserve(session)
    canary = await _agent(
        session,
        hotkey=_CANARY,
        sha256=_ARTIFACT,
        composite=0.9,
        status=AgentStatus.EVALUATING,
    )
    ordinary = await _agent(
        session,
        hotkey=_ORDINARY,
        sha256="a1" * 32,
        composite=0.5,
        status=AgentStatus.EVALUATING,
    )
    rows = {row.agent_id: row for row, _ in await list_provisional_ledger(session)}
    assert rows[canary.agent_id].eligible is False
    assert rows[canary.agent_id].competition_excluded is True
    assert rows[ordinary.agent_id].eligible is True
    assert rows[ordinary.agent_id].competition_excluded is False


async def test_reservation_must_precede_scores_and_is_unique(
    session: AsyncSession,
) -> None:
    await _agent(session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9)
    with pytest.raises(NoncompetitiveExclusionError):
        await _reserve(session)
    await _reserve(session, sha256=_DRIFTED_ARTIFACT)
    with pytest.raises(NoncompetitiveExclusionError):
        await _reserve(session, sha256=_DRIFTED_ARTIFACT)
    for invalid in ({"sha256": "AB" * 32}, {"hotkey": " padded"}):
        with pytest.raises(NoncompetitiveExclusionError):
            await _reserve(session, **invalid)


async def test_exclusions_are_append_only_and_bind_once(session: AsyncSession) -> None:
    exclusion = await _reserve(session)
    canary = await _agent(session, hotkey=_CANARY, sha256=_ARTIFACT, composite=0.9)
    await _bind(session, exclusion, canary)
    with pytest.raises(NoncompetitiveExclusionError):
        await _bind(session, exclusion, canary)
    for statement in (
        "UPDATE noncompetitive_agent_exclusions SET reason = 'quietly lifted now'",
        "UPDATE noncompetitive_agent_exclusions SET agent_id = NULL, "
        "screened_image_sha256 = NULL, bound_by = NULL, bound_reason = NULL, "
        "bound_at = NULL",
        "DELETE FROM noncompetitive_agent_exclusions",
    ):
        await _fresh(session)
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text(statement))
    assert len(await list_team_canary_exclusions(session)) == 1
