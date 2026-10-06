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

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.emission_eligibility import EmissionEligibilitySettings
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import eligibility_checksum
from ditto.api_server.endpoints.scoring import (
    materialize_ledger_snapshot,
    resolve_ledger_context,
)
from ditto.api_server.ledger_pin import (
    classify_vector_against_pins,
    pin_expected_shares,
)
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    AthReview,
    BenchmarkRollout,
    ContinualRetestSettingsRevision,
    EmissionEligibilitySettingsRevision,
    Score,
    ValidatorHeartbeat,
)
from ditto.db.queries.emission_eligibility import list_shadow_records
from ditto.db.queries.ledger_epochs import latest_pin
from ditto.tests.api_server.endpoints.test_scoring import (
    _install_chain,
    _install_epoch_chain,
    _ledger_headers,
    _schedule,
    _scorer_capabilities,
)

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
    agent_id: UUID | None = None,
    created_at: datetime = _NOW - timedelta(days=3),
) -> UUID:
    agent_id = agent_id or uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256=sha256,
                status=AgentStatus.SCORED,
                created_at=created_at,
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


async def _fleet(
    session: AsyncSession, *, protocol: int = 28, seen_at: datetime = _NOW
) -> None:
    """The live weight-setting fleet. ``enforce`` filters the pool only once
    every member reports protocol 28, the fold that reads a provisional
    incumbent; an empty fleet is not ready."""
    async with session.begin():
        await session.merge(
            ValidatorHeartbeat(
                validator_hotkey=_VALIDATOR,
                software_version="0.320.0",
                protocol_version=protocol,
                code_digest="ab" * 32,
                state="idle",
                reported_at=seen_at,
                seen_at=seen_at,
                signature="cd" * 64,
                capabilities=_scorer_capabilities(seen_at, versions=[_BENCH_VERSION]),
            )
        )


async def _unverified_weight_setter(
    session: AsyncSession, *, seen_at: datetime = _NOW
) -> None:
    """A live validator still folds weights when its scorer is unreachable."""
    async with session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=_RUNNER_UP_HOTKEY,
                software_version="0.321.4",
                protocol_version=27,
                code_digest="ef" * 32,
                state="idle",
                reported_at=seen_at,
                seen_at=seen_at,
                signature="ab" * 64,
                capabilities={
                    "scorer_benchmarks": {
                        "status": "unreachable",
                        "supported_bench_versions": [],
                    }
                },
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
        await _fleet(session)
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
        await _fleet(session)
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
        await _fleet(session)
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
    async def test_unverified_protocol_27_weight_setter_stays_payable_on_board(
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
        seen_at = datetime.now(UTC)
        await _fleet(session, seen_at=seen_at)
        await _unverified_weight_setter(session, seen_at=seen_at)
        app.state.emission_eligibility.invalidate()

        response = await client.get("/api/v1/public/leaderboard")
        assert response.status_code == 200, response.text
        held = next(
            entry
            for entry in response.json()["entries"]
            if entry["agent_id"] == str(leader)
        )
        assert held["reward_eligibility"]["enforcement"] == "shadow"
        assert held["reward_eligibility"]["reward_eligible"] is True

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
        await _fleet(session, seen_at=datetime.now(UTC))
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


class TestFleetGate:
    async def test_unverified_protocol_27_weight_setter_keeps_enforcement_in_shadow(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _fleet(session)
        await _unverified_weight_setter(session)
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

        snapshot = await _snapshot(app, session)
        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert snapshot.reward_eligibility_mode is None
        assert snapshot.fleet_readiness is not None
        assert snapshot.fleet_readiness["reward_eligibility"] is False

    async def test_enforce_ahead_of_the_fleet_rehearses_exactly_like_shadow(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A protocol-27 validator ignores ``provisional_incumbent`` and would
        crown and pay a held incumbent's runner-up, so ``enforce`` filters
        nothing until the whole live weight-setting fleet reports 28."""
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _hold(session, leader, kind="deferred_source_review")
        await _fleet(session, protocol=27)
        unfiltered = await _snapshot(app, session)

        async with session_maker() as write_session:
            await _set_posture(write_session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()
        with caplog.at_level(logging.WARNING):
            snapshot = await _snapshot(app, session)

        assert [entry.agent_id for entry in snapshot.entries] == [leader, runner_up]
        assert [entry.model_dump() for entry in snapshot.entries] == [
            entry.model_dump() for entry in unfiltered.entries
        ]
        assert snapshot.reward_eligibility_mode is None
        assert not snapshot.withheld_entries
        assert snapshot.fleet_readiness is not None
        assert snapshot.fleet_readiness["reward_eligibility"] is False
        assert snapshot.fleet_readiness["crown_incumbent"] is True
        # Rehearsed, and recorded as the shadow it effectively is.
        records = await list_shadow_records(session)
        assert {(record.agent_id, record.enforcement) for record in records} == {
            (leader, "shadow")
        }
        gated = [
            record
            for record in caplog.records
            if "does not report protocol 28" in record.getMessage()
        ]
        assert len(gated) == 1

    async def test_a_ready_fleet_records_its_readiness(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _two_miners(session)
        await _fleet(session)
        snapshot = await _snapshot(app, session)
        assert snapshot.fleet_readiness is not None
        assert snapshot.fleet_readiness["reward_eligibility"] is True
        # Off by default: readiness alone changes nothing.
        assert snapshot.reward_eligibility_mode is None
        assert len(snapshot.entries) == 2

    async def test_withheld_entries_are_built_exactly_like_payable_ones(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A pin's withheld entry is the one the artifact would carry with the
        gate off, so a provisional incumbent folds on the same numbers it was
        crowned on. Live reads never build them."""
        _install(app, session_maker)
        leader, runner_up = await _two_miners(session)
        await _fleet(session)
        async with session.begin():
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings=ContinualRetestSettings(
                        crown_incumbent_mode="fleet_ready"
                    ).model_dump(mode="json"),
                    checksum="ab" * 32,
                    reason="defend the crown from the served incumbent",
                    actor="operator@example.com",
                )
            )
        app.state.continual_retest_settings.invalidate()

        async def pin_snapshot() -> Any:
            async with session_maker() as pin_session:
                context = await resolve_ledger_context(app.state, pin_session, now=_NOW)
                return await materialize_ledger_snapshot(
                    app.state,
                    pin_session,
                    context=context,
                    now=_NOW,
                    requesting_validator_hotkey=None,
                )

        ungated = await pin_snapshot()
        await _hold(session, leader, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()
        gated = await pin_snapshot()

        assert [entry.agent_id for entry in gated.entries] == [runner_up]
        (withheld,) = gated.withheld_entries
        assert withheld.model_dump() == ungated.entries[0].model_dump()
        assert gated.owner_roots[leader] == ungated.owner_roots[leader]
        assert not (await _snapshot(app, session)).withheld_entries


_GOLDEN = (
    Path(__file__).resolve().parents[6]
    / "ditto"
    / "tests"
    / "fixtures"
    / "provisional_incumbent_pin.json"
)
"""The pinned ledger this module serves for the held-incumbent scenario, as the
validator receives it. ``ditto/tests/validator/test_provisional_incumbent.py``
folds these exact bytes through the real validator worker, so the two suites
together cover materialize -> pin -> served ledger -> validator fold."""

# Wire fields that depend on the wall clock or the database row identity rather
# than on the fold input.
_VOLATILE = frozenset(
    {"generated_at", "age_seconds", "ledger_snapshot_id", "pinned_at"}
)

_HELD_ID = UUID("00000000-0000-4000-8000-0000000000a1")
_RUNNER_UP_ID = UUID("00000000-0000-4000-8000-0000000000b2")


class TestProvisionalIncumbent:
    """Peyton's P1 on #2123: a held champion keeps the crown and its 65% slot
    goes unpaid rather than reassigned to the eligible runner-up."""

    async def _arm(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        async with session.begin():
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings=ContinualRetestSettings(
                        crown_incumbent_mode="fleet_ready"
                    ).model_dump(mode="json"),
                    checksum="ab" * 32,
                    reason="defend the crown from the served incumbent",
                    actor="operator@example.com",
                )
            )
        await _fleet(session, seen_at=now)
        _install(app, session_maker)
        _install_chain(app)
        app.state.continual_retest_settings.invalidate()

    def _epochs(self, app: FastAPI) -> AsyncMock:
        """The pin's chain epoch, plus a metagraph registering both miners so
        the public board projects emissions for them."""
        read = _install_epoch_chain(app, _schedule(25_028, block=9_033_471))
        app.state.chain.get_recent_neurons = AsyncMock(
            return_value=[
                NeuronInfo(
                    hotkey=hotkey,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=uid,
                    stake=0.0,
                    validator_permit=False,
                )
                for uid, hotkey in enumerate((_LEADER_HOTKEY, _RUNNER_UP_HOTKEY))
            ]
        )
        return read

    async def _read(self, client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.get("/api/v1/scoring/scores", headers=_ledger_headers())
        assert response.status_code == 200, response.text
        return response.json()

    async def _hold_and_enforce(
        self, app: FastAPI, session: AsyncSession, agent_id: UUID
    ) -> None:
        await _hold(session, agent_id, kind="deferred_source_review")
        await _set_posture(session, enforcement="enforce")
        app.state.emission_eligibility.invalidate()

    async def test_rotated_held_family_keeps_the_same_anchor_on_board_and_pin(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ditto.api_server.endpoints import public as public_endpoint
        from ditto.tests.api_server.endpoints.test_public import _seed_payment

        await self._arm(app, session, session_maker)
        retired_at = _NOW - timedelta(days=4)
        retired = await _seed(
            session,
            hotkey=_LEADER_HOTKEY,
            name="retired generation",
            composite=0.95,
            sha256="ab" * 32,
            created_at=retired_at,
        )
        rotated = await _seed(
            session,
            hotkey=_RUNNER_UP_HOTKEY,
            name="registered generation",
            composite=0.95,
            sha256="cd" * 32,
            created_at=_NOW - timedelta(days=3),
        )
        for index, (agent_id, hotkey) in enumerate(
            ((retired, _LEADER_HOTKEY), (rotated, _RUNNER_UP_HOTKEY)), start=41
        ):
            await _seed_payment(
                session_maker,
                agent_id=str(agent_id),
                miner_hotkey=hotkey,
                miner_coldkey="5SharedHeldFamilyColdkey",
                index=index,
            )
        read = self._epochs(app)
        await self._read(client)
        await _hold(session, retired, kind="deferred_source_review")
        await self._hold_and_enforce(app, session, rotated)
        app.state.chain.get_recent_neurons.return_value = [
            NeuronInfo(
                hotkey=_RUNNER_UP_HOTKEY,
                coldkey="5SharedHeldFamilyColdkey",
                uid=0,
                stake=0.0,
                validator_permit=False,
            )
        ]
        app.state.public_registration_snapshot = None
        real_projection = public_endpoint._public_koth_emissions
        projected = []

        def observe(*args: Any, **kwargs: Any):
            projected.append(kwargs["provisional_incumbent"])
            return real_projection(*args, **kwargs)

        monkeypatch.setattr(public_endpoint, "_public_koth_emissions", observe)
        board_response = await client.get("/api/v1/public/leaderboard")
        assert board_response.status_code == 200, board_response.text
        assert projected[0].agent_id == rotated
        assert projected[0].fold_first_seen == retired_at
        assert board_response.json()["emissions"]["champion_agent_id"] == str(rotated)

        read.return_value = _schedule(25_029, block=9_033_831)
        served = await self._read(client)
        held = served["provisional_incumbent"]
        assert held["agent_id"] == str(rotated)
        assert datetime.fromisoformat(held["first_seen"].replace("Z", "+00:00")) == (
            projected[0].fold_first_seen
        )

    async def test_active_pin_labels_freeze_while_next_pin_projection_updates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._arm(app, session, session_maker)
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        await _seed(
            session,
            hotkey=_LEADER_HOTKEY,
            name="leader",
            composite=0.95,
            sha256="ab" * 32,
            agent_id=_HELD_ID,
            created_at=_NOW - timedelta(days=4),
        )
        await _seed(
            session,
            hotkey=_RUNNER_UP_HOTKEY,
            name="runner-up",
            composite=0.80,
            sha256="cd" * 32,
            agent_id=_RUNNER_UP_ID,
            created_at=_NOW - timedelta(days=3),
        )
        read = self._epochs(app)
        first = await self._read(client)
        assert [entry["agent_id"] for entry in first["entries"]] == [
            str(_HELD_ID),
            str(_RUNNER_UP_ID),
        ]

        # A review opens and policy becomes enforce inside the same chain epoch.
        # Validators still receive the previous pin, so the public and operator
        # reads must describe that pin rather than the next fold's candidate.
        await self._hold_and_enforce(app, session, _HELD_ID)
        same_epoch = await self._read(client)
        assert same_epoch["ledger_digest"] == first["ledger_digest"]
        board = (await client.get("/api/v1/public/leaderboard")).json()
        by_id = {row["agent_id"]: row for row in board["entries"]}
        assert by_id[str(_HELD_ID)].get("reward_eligibility") is None
        # Emissions explicitly project the next pin, which can already show
        # the new policy while row labels still describe the active pin.
        assert board["emissions"]["reward_eligibility_mode"] == "enforce"
        assert board["emissions"]["provisional_champion"] is True
        pipeline = (
            await client.get(f"/api/v1/public/agent/{_HELD_ID}/pipeline")
        ).json()
        assert pipeline.get("reward_eligibility") is None
        operator = (
            await client.get(
                f"/api/v1/admin/agents/{_HELD_ID}/emission-eligibility",
                headers={
                    "Authorization": "Bearer test-admin-token-at-least-32-characters"
                },
            )
        ).json()
        assert operator["in_ledger"] is True
        assert operator["eligibility"]["enforcement"] == "off"
        assert operator["effective"]["effective_enforcement"] == "off"

        read.return_value = _schedule(25_029, block=9_033_831)
        next_epoch = await self._read(client)
        assert [row["agent_id"] for row in next_epoch["entries"]] == [
            str(_RUNNER_UP_ID)
        ]
        board = (await client.get("/api/v1/public/leaderboard")).json()
        by_id = {row["agent_id"]: row for row in board["entries"]}
        assert by_id[str(_HELD_ID)]["reward_eligibility"]["reward_eligible"] is False
        assert board["emissions"]["provisional_champion"] is True
        operator = (
            await client.get(
                f"/api/v1/admin/agents/{_HELD_ID}/emission-eligibility",
                headers={
                    "Authorization": "Bearer test-admin-token-at-least-32-characters"
                },
            )
        ).json()
        assert operator["in_ledger"] is False
        assert operator["eligibility"]["enforcement"] == "enforce"
        assert operator["effective"]["effective_enforcement"] == "enforce"

        # A terminal clear inside this epoch likewise cannot rewrite the
        # already served pin or claim that its unpaid crown is now paid.
        async with session.begin():
            review = await session.scalar(
                select(AthReview).where(AthReview.agent_id == _HELD_ID)
            )
            assert review is not None
            review.status = "resolved"
            review.resolution = "clear"
            review.resolved_by = "operator@example.com"
            review.resolution_reason = "cleared after exact artifact review"
            review.resolved_at = datetime.now(UTC)
        still_pinned = await self._read(client)
        assert still_pinned["ledger_digest"] == next_epoch["ledger_digest"]
        board = (await client.get("/api/v1/public/leaderboard")).json()
        by_id = {row["agent_id"]: row for row in board["entries"]}
        assert by_id[str(_HELD_ID)]["reward_eligibility"]["reward_eligible"] is False
        assert board["emissions"]["provisional_champion"] is True

    async def test_held_incumbent_keeps_the_crown_and_its_share_burns(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._arm(app, session, session_maker)
        await _seed(
            session,
            hotkey=_LEADER_HOTKEY,
            name="leader",
            composite=0.95,
            sha256="ab" * 32,
            agent_id=_HELD_ID,
            created_at=_NOW - timedelta(days=4),
        )
        await _seed(
            session,
            hotkey=_RUNNER_UP_HOTKEY,
            name="runner-up",
            composite=0.80,
            sha256="cd" * 32,
            agent_id=_RUNNER_UP_ID,
            created_at=_NOW - timedelta(days=3),
        )
        read = self._epochs(app)
        first = await self._read(client)
        assert [entry["agent_id"] for entry in first["entries"]] == [
            str(_HELD_ID),
            str(_RUNNER_UP_ID),
        ]
        async with session_maker() as pin_session:
            first_pin = await latest_pin(pin_session, netuid=118)
        assert first_pin is not None and first_pin.champion_agent_id == _HELD_ID

        # The champion's exact artifact goes under review, the gate enforces.
        await self._hold_and_enforce(app, session, _HELD_ID)
        read.return_value = _schedule(25_029, block=9_033_831)
        served = await self._read(client)

        # The validator ledger: the held champion is not payable ...
        assert [entry["agent_id"] for entry in served["entries"]] == [
            str(_RUNNER_UP_ID)
        ]
        assert served["reward_eligibility_mode"] == "enforce"
        # ... but it is still the crown incumbent the fold defends.
        assert served["crown_mode"] == "incumbent"
        assert served["crown_incumbent_agent_id"] == str(_HELD_ID)
        assert served["provisional_incumbent"]["agent_id"] == str(_HELD_ID)
        assert served["provisional_incumbent"]["sha256"] == "ab" * 32

        async with session_maker() as pin_session:
            pin = await latest_pin(pin_session, netuid=118)
        assert pin is not None and pin.epoch_index == 25_029
        # The crown carries to the next pin; nobody inherited it.
        assert pin.champion_agent_id == _HELD_ID
        assert pin.incumbent_agent_id == _HELD_ID
        shares = pin_expected_shares(pin)
        # The runner-up keeps exactly the tail share it held while the crown was
        # paid; the champion's 0.65 / 0.79 of the miner pool burns.
        assert shares == {_RUNNER_UP_HOTKEY: pytest.approx(0.14 / 0.79, abs=1e-12)}

        # The same bytes the validator suite folds (see ``_GOLDEN``).
        fold_input = {k: v for k, v in served.items() if k not in _VOLATILE}
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert fold_input == golden["ledger"]
        assert shares == golden["expected_miner_shares"]
        assert golden["champion_agent_id"] == str(_HELD_ID)
        # A protocol-28 validator's vector for that pin reads as agreeing.
        burn = "5" + "Z" * 47
        assert (
            classify_vector_against_pins(
                {**shares, burn: 1.0 - sum(shares.values())},
                expected_current=shares,
                expected_previous=None,
                burn_hotkey=burn,
            )
            == "current"
        )

        # The public projection tells the same story from the same pin.
        board = (await client.get("/api/v1/public/leaderboard")).json()
        emissions = board["emissions"]
        assert emissions["champion_agent_id"] == str(_HELD_ID)
        assert emissions["provisional_champion"] is True
        assert emissions["champion_reward_eligible"] is False
        assert emissions["reward_eligibility_mode"] == "enforce"
        assert emissions["crown_incumbent_agent_id"] == str(_HELD_ID)
        recipients = {item["agent_id"]: item for item in emissions["recipients"]}
        held = recipients[str(_HELD_ID)]
        assert held["role"] == "champion"
        assert held["paid"] is False
        # Shares stay fractions of the whole miner pool, so the unpaid crown is
        # visible rather than renormalized away.
        assert held["share_of_miner_pool"] == pytest.approx(0.65 / 0.79)
        runner = recipients[str(_RUNNER_UP_ID)]
        assert "paid" not in runner
        assert runner["share_of_miner_pool"] == pytest.approx(shares[_RUNNER_UP_HOTKEY])
        # Score rank is untouched: the held leader still leads the board.
        by_id = {entry["agent_id"]: entry for entry in board["entries"]}
        assert by_id[str(_HELD_ID)]["rank"] == 1

        epochs = (await client.get("/api/v1/public/ledger-epochs")).json()["epochs"]
        latest = epochs[0]
        assert latest["epoch_index"] == 25_029
        assert latest["champion"]["agent_id"] == str(_HELD_ID)
        assert latest["crown_changed"] is False
        epoch_recipients = {item["agent_id"]: item for item in latest["recipients"]}
        assert epoch_recipients[str(_HELD_ID)]["paid"] is False
        assert "paid" not in epoch_recipients[str(_RUNNER_UP_ID)]

    async def test_a_challenger_that_clears_the_band_takes_the_paid_crown(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._arm(app, session, session_maker)
        held = await _seed(
            session,
            hotkey=_LEADER_HOTKEY,
            name="leader",
            composite=0.80,
            sha256="ab" * 32,
            created_at=_NOW - timedelta(days=4),
        )
        read = self._epochs(app)
        await self._read(client)
        challenger = await _seed(
            session,
            hotkey=_RUNNER_UP_HOTKEY,
            name="challenger",
            composite=0.95,
            sha256="cd" * 32,
            created_at=_NOW - timedelta(days=1),
        )
        await self._hold_and_enforce(app, session, held)
        read.return_value = _schedule(25_029, block=9_033_831)
        served = await self._read(client)

        assert [entry["agent_id"] for entry in served["entries"]] == [str(challenger)]
        assert served["provisional_incumbent"]["agent_id"] == str(held)
        async with session_maker() as pin_session:
            pin = await latest_pin(pin_session, netuid=118)
        assert pin is not None
        assert pin.champion_agent_id == challenger
        # Crowned and paid the champion share; the held incumbent's tail slot
        # burns rather than topping the champion up.
        assert pin_expected_shares(pin) == {
            _RUNNER_UP_HOTKEY: pytest.approx(0.65 / 0.79, abs=1e-12)
        }

        # The pin's own recipients: the held incumbent's tail slot is unpaid.
        epochs = (await client.get("/api/v1/public/ledger-epochs")).json()["epochs"]
        latest = epochs[0]
        assert latest["champion"]["agent_id"] == str(challenger)
        assert latest["crown_changed"] is True
        recipients = {item["agent_id"]: item for item in latest["recipients"]}
        assert recipients[str(held)]["role"] == "tail"
        assert recipients[str(held)]["paid"] is False
        assert "paid" not in recipients[str(challenger)]

        # The board projects the NEXT pin, whose incumbent is the paid
        # challenger: the held row is no longer the incumbent, so it leaves the
        # fold like any other withheld row.
        emissions = (await client.get("/api/v1/public/leaderboard")).json()["emissions"]
        assert emissions["champion_agent_id"] == str(challenger)
        assert emissions["champion_reward_eligible"] is True
        assert emissions["provisional_champion"] is False
        assert [item["agent_id"] for item in emissions["recipients"]] == [
            str(challenger)
        ]
        assert all("paid" not in item for item in emissions["recipients"])

    async def test_a_held_non_incumbent_never_reaches_the_fold(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only the incumbent can be provisional: any other withheld row stays
        out of the pool exactly as before, and the pin carries nothing new."""
        await self._arm(app, session, session_maker)
        champion, held = await _two_miners(session)
        read = self._epochs(app)
        await self._read(client)
        await self._hold_and_enforce(app, session, held)
        read.return_value = _schedule(25_029, block=9_033_831)
        served = await self._read(client)

        assert [entry["agent_id"] for entry in served["entries"]] == [str(champion)]
        assert "provisional_incumbent" not in served
        assert served["crown_incumbent_agent_id"] == str(champion)
        emissions = (await client.get("/api/v1/public/leaderboard")).json()["emissions"]
        assert emissions["champion_agent_id"] == str(champion)
        assert emissions["provisional_champion"] is False
        assert [item["agent_id"] for item in emissions["recipients"]] == [str(champion)]
        assert all("paid" not in item for item in emissions["recipients"])

    async def test_enforce_ahead_of_the_fleet_serves_the_unfiltered_pin(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._arm(app, session, session_maker)
        await _fleet(session, protocol=27, seen_at=datetime.now(UTC))
        leader, runner_up = await _two_miners(session)
        read = self._epochs(app)
        before = await self._read(client)
        await self._hold_and_enforce(app, session, leader)
        read.return_value = _schedule(25_029, block=9_033_831)
        after = await self._read(client)

        assert [entry["agent_id"] for entry in after["entries"]] == [
            str(leader),
            str(runner_up),
        ]
        assert "reward_eligibility_mode" not in after
        assert "provisional_incumbent" not in after
        # Nothing about the fold input moved: same entries, same markers.
        assert after["entries"] == before["entries"]
        assert after["crown_incumbent_agent_id"] == str(leader)
        async with session_maker() as pin_session:
            pin = await latest_pin(pin_session, netuid=118)
        assert pin is not None
        assert pin.context["fleet"]["reward_eligibility"] is False
        emissions = (await client.get("/api/v1/public/leaderboard")).json()["emissions"]
        assert emissions["champion_agent_id"] == str(leader)
        assert emissions["provisional_champion"] is False
        assert "reward_eligibility_mode" not in emissions
