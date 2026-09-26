"""End to end: an unresolved top scorer cannot be paid, and still shows a score.

This is #2041's acceptance, asserted against the two surfaces that matter:

* ``materialize_ledger_snapshot`` -- the exact function whose output every
  validator folds into weights.
* ``GET /api/v1/public/leaderboard`` -- what a miner reads, which must keep
  publishing the score and its rank while saying it is not earning.

The same posture and the same review rows drive both, which is the issue's
requirement that the fold and the public projection consume one eligibility
record.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.emission_eligibility import EmissionEligibilitySettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import eligibility_checksum
from ditto.api_server.endpoints.scoring import (
    materialize_ledger_snapshot,
    resolve_ledger_context,
)
from ditto.db.models import (
    Agent,
    AthReview,
    BenchmarkRollout,
    EmissionEligibilitySettingsRevision,
    Score,
)
from ditto.db.queries.emission_eligibility import list_shadow_records

pytestmark = pytest.mark.asyncio

_BENCH_VERSION = 7
_NOW = datetime(2026, 9, 23, 14, 37, 11, tzinfo=UTC)
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_LEADER_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_RUNNER_UP_HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


@pytest.fixture(autouse=True)
async def _live_era_has_activated(session: AsyncSession) -> None:
    """A freshly migrated database has never activated a rollout, so every
    default ledger read would resolve to an empty era. Plant the activation the
    fleet has actually had, exactly as ``tests/db/queries/test_scores.py`` does.
    """
    async with session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_BENCH_VERSION - 1,
                desired_version=_BENCH_VERSION,
                status="activated",
                cohort_size=5,
                created_at=_NOW - timedelta(days=30),
                activated_at=_NOW - timedelta(days=29),
            )
        )


async def _seed(
    session: AsyncSession,
    *,
    hotkey: str,
    name: str,
    composite: float,
    sha256: str,
) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256=sha256,
                status=AgentStatus.SCORED,
                created_at=_NOW - timedelta(days=3),
            )
        )
        await session.flush()
        for index in range(3):
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=f"{_VALIDATOR[:-1]}{index}",
                    run_id=f"{name}-{index}",
                    seed=42,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=500,
                    n=114,
                    generated_at=_NOW - timedelta(days=2),
                )
            )
    return agent_id


async def _hold(session: AsyncSession, agent_id: UUID, *, kind: str) -> None:
    """A stranded hold: the review is open while ``agents.status`` is ``scored``.

    This is the state the gate exists for; ``list_eligible_ledger``'s status
    filter does not catch it.
    """
    async with session.begin():
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent_id,
                status="pending",
                opened_at=_NOW - timedelta(hours=4),
                original_reason="held for source review",
                original_policy_version=1,
                original_evidence={},
                algorithm_provenance={"review_kind": kind},
            )
        )


async def _set_posture(session: AsyncSession, **fields: object) -> None:
    settings = EmissionEligibilitySettings(**fields)  # type: ignore[arg-type]
    async with session.begin():
        session.add(
            EmissionEligibilitySettingsRevision(
                parent_revision=0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum=eligibility_checksum(settings),
                reason="test posture for the terminal review gate",
                actor="operator@example.com",
            )
        )


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as request_session:
            yield request_session

    app.dependency_overrides[get_session] = _session
    app.state.emission_eligibility.invalidate()


async def _snapshot(app: FastAPI, session: AsyncSession):
    context = await resolve_ledger_context(app.state, session, now=_NOW)
    return await materialize_ledger_snapshot(
        app.state,
        session,
        context=context,
        now=_NOW,
        requesting_validator_hotkey=_VALIDATOR,
    )


async def _two_miners(session: AsyncSession) -> tuple[UUID, UUID]:
    leader = await _seed(
        session,
        hotkey=_LEADER_HOTKEY,
        name="leader",
        composite=0.95,
        sha256="ab" * 32,
    )
    runner_up = await _seed(
        session,
        hotkey=_RUNNER_UP_HOTKEY,
        name="runner-up",
        composite=0.80,
        sha256="cd" * 32,
    )
    return leader, runner_up


class TestValidatorLedger:
    async def test_off_pays_the_unresolved_leader_exactly_as_before(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")

        snapshot = await _snapshot(app, session)
        # No revision stored: the gate is off, so this is the pre-#2041 pool.
        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert snapshot.reward_eligibility_mode is None
        assert await list_shadow_records(session) == []

    async def test_shadow_still_pays_but_records_what_it_would_have_dropped(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="shadow")
        app.state.emission_eligibility.invalidate()

        snapshot = await _snapshot(app, session)
        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert snapshot.reward_eligibility_mode is None

        records = await list_shadow_records(session)
        # The rehearsal an operator reads before flipping the switch: the exact
        # artifact, why, and under which posture revision.
        assert [record.agent_id for record in records] == [leader]
        assert records[0].state == "unresolved_review"
        assert records[0].artifact_sha256 == "ab" * 32
        assert records[0].bench_version == _BENCH_VERSION
        assert records[0].enforcement == "shadow"

    async def test_enforce_excludes_the_unresolved_leader_from_the_fold(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        snapshot = await _snapshot(app, session)
        # #2041 acceptance: "An unresolved top scorer cannot receive emissions."
        assert [entry.agent_id for entry in snapshot.entries] == [runner_up]
        assert snapshot.reward_eligibility_mode == "enforce"
        # The withheld artifact is still recorded, so the audit trail explains
        # why the fold skipped it.
        assert [record.agent_id for record in await list_shadow_records(session)] == [
            leader
        ]

    async def test_an_escalated_leader_is_also_excluded(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="anomalous_score")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        snapshot = await _snapshot(app, session)
        assert [entry.agent_id for entry in snapshot.entries] == [runner_up]
        records = await list_shadow_records(session)
        assert records[0].state == "review_escalated"

    async def test_a_clear_recorded_this_window_is_not_paid_until_the_next(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        # The operator clears the hold five minutes into the 14:00 window.
        cleared_at = datetime(2026, 9, 23, 14, 5, 0, tzinfo=UTC)
        async with session.begin():
            review = await session.scalar(
                select(AthReview).where(AthReview.agent_id == leader)
            )
            assert review is not None
            review.status = "resolved"
            review.resolution = "clear"
            review.resolution_reason = "reviewed and cleared"
            review.resolved_at = cleared_at
            review.resolved_by = "operator@example.com"

        context = await resolve_ledger_context(app.state, session, now=_NOW)
        same_window = await materialize_ledger_snapshot(
            app.state,
            session,
            context=context,
            now=_NOW,
            requesting_validator_hotkey=_VALIDATOR,
        )
        # Still withheld: a clear inside the window a validator is already
        # folding must not move the pool under it, and paying from before the
        # boundary would be retroactive.
        assert [entry.agent_id for entry in same_window.entries] == [runner_up]
        records = await list_shadow_records(session)
        assert records[0].state == "awaiting_next_window"

        next_window = await materialize_ledger_snapshot(
            app.state,
            session,
            context=context,
            now=datetime(2026, 9, 23, 15, 0, 30, tzinfo=UTC),
            requesting_validator_hotkey=_VALIDATOR,
        )
        # Eligible from the boundary onward, with nothing owed for the held
        # period: there is no back-pay field anywhere in this payload.
        assert [entry.agent_id for entry in next_window.entries] == [leader, runner_up]

    async def test_a_malformed_posture_keeps_paying(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        async with session.begin():
            session.add(
                EmissionEligibilitySettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={"enforcement": "obliterate"},
                    checksum="0" * 64,
                    reason="a revision the platform cannot parse",
                    actor="operator@example.com",
                )
            )
        app.state.emission_eligibility.invalidate()

        snapshot = await _snapshot(app, session)
        # The documented safe default is to keep paying miners, not to withhold
        # from them because a row will not parse.
        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert snapshot.reward_eligibility_mode is None


class TestPublicBoard:
    async def test_the_board_keeps_the_score_and_says_why_it_is_not_earning(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        response = await client.get("/api/v1/public/leaderboard")
        assert response.status_code == 200, response.text
        body = response.json()
        by_id = {entry["agent_id"]: entry for entry in body["entries"]}
        held = by_id[str(leader)]

        # "Keep benchmark scores visible while review is pending."
        assert held["composite"] == pytest.approx(0.95)
        # Rank is score rank and stays the score's: the leader still leads.
        assert held["rank"] == 1
        # Reward eligibility is a separate field with a readable reason.
        eligibility = held["reward_eligibility"]
        assert eligibility["state"] == "unresolved_review"
        assert eligibility["reward_eligible"] is False
        assert eligibility["posture_satisfied"] is False
        assert eligibility["enforcement"] == "enforce"
        assert "emissions wait" in eligibility["reason"]
        assert held["emission_eligible"] is not True

        # While the gate is enforcing every row states its status, so "earning"
        # is a published fact rather than the absence of a warning.
        clean = by_id[str(runner_up)]
        assert clean["reward_eligibility"]["state"] == "eligible"
        assert clean["reward_eligibility"]["reward_eligible"] is True
        assert clean["emission_eligible"] is not False

    async def test_board_carries_no_eligibility_annotation_while_the_gate_is_off(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, _ = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")

        response = await client.get("/api/v1/public/leaderboard")
        assert response.status_code == 200, response.text
        entries = response.json()["entries"]
        # A platform that merely merged this serves the pre-#2041 payload.
        assert all(entry.get("reward_eligibility") is None for entry in entries)

    async def test_the_submission_page_explains_a_withheld_score(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, _ = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        response = await client.get(f"/api/v1/public/agent/{leader}/pipeline")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reward_eligibility"]["state"] == "unresolved_review"
        assert body["reward_eligibility"]["activates_at"] is None
        # The score history is untouched by the gate.
        assert body["score_count"] == 3
