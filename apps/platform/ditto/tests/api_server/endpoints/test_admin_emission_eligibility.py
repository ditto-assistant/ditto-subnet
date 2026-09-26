"""Contract tests for the operator control over the terminal-review gate.

The write path is guarded exactly like the burn control, and for the same
reason: it decides who is paid. The reads are what an operator rehearses the
switch from, and what a miner appeal is answered with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.emission_eligibility import window_start
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    EmissionEligibilityShadowRecord,
    Score,
)
from ditto.db.queries.emission_eligibility import (
    count_shadow_records_in_window,
    list_shadow_records,
    load_review_postures,
    record_shadow_exclusions,
)
from ditto.db.queries.scores import list_scores_for_agent

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/emission-eligibility"
_CONFIRMATION = "APPLY EMISSION ELIGIBILITY"
_MINER = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_NOW = datetime(2026, 9, 23, 14, 37, 11, tzinfo=UTC)
_SHA = "cd" * 32


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.state.emission_eligibility.invalidate()


def _payload(
    *, expected_revision: int = 0, enforcement: str = "shadow", **overrides: object
) -> dict[str, object]:
    settings: dict[str, object] = {"enforcement": enforcement}
    settings.update(overrides)
    return {
        "expected_revision": expected_revision,
        "scope": "*",
        "settings": settings,
        "reason": "owner-approved rehearsal of the terminal-review gate",
        "actor": "operator@example.com",
        "confirmation": _CONFIRMATION,
    }


async def _seed_scored_agent(
    session: AsyncSession, *, sha256: str = _SHA, composite: float = 0.9
) -> UUID:
    agent_id = uuid4()
    async with session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_MINER,
                name="alpha",
                sha256=sha256,
                status=AgentStatus.SCORED,
                created_at=_NOW - timedelta(days=3),
            )
        )
        await session.flush()
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=7,
                validator_hotkey=_VALIDATOR,
                run_id="run_1",
                seed=42,
                composite=composite,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=500,
                n=114,
                generated_at=_NOW - timedelta(days=2),
            )
        )
    return agent_id


async def _open_hold(session: AsyncSession, agent_id: UUID) -> UUID:
    """A stranded hold: pending review, ``agents.status`` still ``scored``.

    Exactly the production state ``docs/ath-review-queue.md`` documents and the
    one this gate exists for.
    """
    review_id = uuid4()
    async with session.begin():
        session.add(
            AthReview(
                review_id=review_id,
                agent_id=agent_id,
                status="pending",
                opened_at=_NOW - timedelta(hours=6),
                original_reason="held for source review",
                original_policy_version=1,
                original_evidence={},
                algorithm_provenance={"review_kind": "deferred_source_review"},
            )
        )
        session.add(
            AthReviewAction(
                action_id=uuid4(),
                review_id=review_id,
                action="reopen",
                reason="opened by the deferred source review",
                actor="platform:deferred-source-review",
                evidence={"sha256": _SHA},
            )
        )
    return review_id


class TestPosture:
    async def test_unconfigured_platform_serves_the_off_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["current"] is None
        assert body["history"] == []
        assert body["default"]["enforcement"] == "off"
        effective = body["effective"]
        # Merging this must not be able to move emissions. That is the whole
        # claim, and it is this assertion.
        assert effective["settings"]["enforcement"] == "off"
        assert effective["source"] == "default"
        assert effective["revision"] == 0
        assert effective["shadow_excluded_count"] == 0
        # The operator is told when a clear recorded now would take effect, so
        # "when does this miner start earning" is not a hand calculation.
        assert effective["next_window_start"] > effective["current_window_start"]
        assert body["confirmation_phrase"] == _CONFIRMATION

    async def test_write_requires_the_typed_confirmation(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        payload = _payload() | {"confirmation": "yes"}
        response = await client.post(_URL, headers=_HEADERS, json=payload)
        assert response.status_code == 409
        assert _CONFIRMATION in response.text

    async def test_write_is_guarded_by_expected_revision(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        stale = await client.post(
            _URL, headers=_HEADERS, json=_payload(expected_revision=9)
        )
        assert stale.status_code == 409
        assert "refresh" in stale.text

    async def test_revisions_are_append_only_and_audited(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        shadow = await client.post(_URL, headers=_HEADERS, json=_payload())
        assert shadow.status_code == 200, shadow.text
        assert shadow.json()["revision"] == 1
        assert shadow.json()["actor"] == "operator@example.com"

        app.state.emission_eligibility.invalidate()
        enforce = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(expected_revision=1, enforcement="enforce"),
        )
        assert enforce.status_code == 200, enforce.text
        assert enforce.json()["revision"] == 2
        assert enforce.json()["parent_revision"] == 1

        listing = await client.get(_URL, headers=_HEADERS)
        body = listing.json()
        assert body["current"]["revision"] == 2
        assert body["effective"]["settings"]["enforcement"] == "enforce"
        assert body["effective"]["source"] == "revision"
        # The rehearsal history is not rewritten by a posture change.
        assert [row["revision"] for row in body["history"]] == [2, 1]

    async def test_window_length_is_bounded_by_the_platform(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        response = await client.post(
            _URL,
            headers=_HEADERS,
            json=_payload(activation_window_seconds=5),
        )
        assert response.status_code == 422


class TestShadowRehearsal:
    async def test_a_windows_finding_is_recorded_once_not_once_per_poll(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_scored_agent(session)
        window = window_start(_NOW, window_seconds=3_600)
        row = {
            "record_id": uuid4(),
            "agent_id": agent_id,
            "artifact_sha256": _SHA,
            "bench_version": 7,
            "state": "unresolved_review",
            "reason": "Source review for this exact artifact is still open.",
            "policy_revision": 1,
            "policy_checksum": "0" * 64,
            "enforcement": "shadow",
            "window_start": window,
        }
        async with session.begin():
            assert await record_shadow_exclusions(session, rows=[row]) == 1
        # Validators poll every 30 seconds. Without the per-window key this
        # table would grow by the withheld set twice a minute.
        async with session.begin():
            assert (
                await record_shadow_exclusions(
                    session, rows=[row | {"record_id": uuid4()}]
                )
                == 0
            )
        assert (
            await session.scalar(
                select(func.count()).select_from(EmissionEligibilityShadowRecord)
            )
            == 1
        )
        assert await count_shadow_records_in_window(session, window_start=window) == 1
        records = await list_shadow_records(session, agent_id=agent_id)
        assert [record.state for record in records] == ["unresolved_review"]

    async def test_a_new_window_records_the_same_finding_again(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_scored_agent(session)
        base = {
            "agent_id": agent_id,
            "artifact_sha256": _SHA,
            "bench_version": 7,
            "state": "review_inconclusive",
            "reason": "Automated source review finished without a finding.",
            "policy_revision": 1,
            "policy_checksum": "0" * 64,
            "enforcement": "shadow",
        }
        first = window_start(_NOW, window_seconds=3_600)
        async with session.begin():
            await record_shadow_exclusions(
                session,
                rows=[base | {"record_id": uuid4(), "window_start": first}],
            )
        async with session.begin():
            inserted = await record_shadow_exclusions(
                session,
                rows=[
                    base
                    | {
                        "record_id": uuid4(),
                        "window_start": first + timedelta(hours=1),
                    }
                ],
            )
        # A finding that persists across windows stays visible per window, so an
        # operator can see how long an artifact has been withheld.
        assert inserted == 1


class TestReviewFacts:
    async def test_a_stranded_hold_is_read_off_the_existing_tables(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_scored_agent(session)
        await _open_hold(session, agent_id)
        postures = await load_review_postures(session, [agent_id])
        posture = postures[agent_id]
        assert posture.review_status == "pending"
        assert posture.review_kind == "deferred_source_review"
        assert posture.review_resolution is None

    async def test_an_agent_with_no_review_reads_as_never_held(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_scored_agent(session)
        posture = (await load_review_postures(session, [agent_id]))[agent_id]
        assert posture.review_status is None
        assert posture.passed_attempt_count == 0

    async def test_rejection_preserves_the_score_and_the_review_history(
        self, session: AsyncSession
    ) -> None:
        agent_id = await _seed_scored_agent(session)
        review_id = await _open_hold(session, agent_id)
        async with session.begin():
            review = await session.get(AthReview, review_id)
            assert review is not None
            review.status = "resolved"
            review.resolution = "reject"
            review.resolution_reason = "adjudicated copy of an earlier submission"
            review.resolved_at = _NOW
            review.resolved_by = "operator@example.com"
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=review_id,
                    action="reject",
                    reason="adjudicated copy of an earlier submission",
                    actor="operator@example.com",
                    evidence={"sha256": _SHA},
                )
            )

        # #2041: "Preserve score and review history after rejection."
        scores = await list_scores_for_agent(session, agent_id=agent_id)
        assert [score.composite for score in scores] == [pytest.approx(0.9)]
        actions = list(
            await session.scalars(
                select(AthReviewAction)
                .where(AthReviewAction.review_id == review_id)
                .order_by(AthReviewAction.created_at)
            )
        )
        assert [action.action for action in actions] == ["reopen", "reject"]
        posture = (await load_review_postures(session, [agent_id]))[agent_id]
        assert posture.review_resolution == "reject"
        assert posture.review_resolved_at is not None


class TestPerAgentRead:
    async def test_the_appeal_read_names_the_state_and_the_pool_membership(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        agent_id = await _seed_scored_agent(session)
        await _open_hold(session, agent_id)
        applied = await client.post(
            _URL, headers=_HEADERS, json=_payload(enforcement="enforce")
        )
        assert applied.status_code == 200, applied.text
        app.state.emission_eligibility.invalidate()

        response = await client.get(
            f"/api/v1/admin/agents/{agent_id}/emission-eligibility",
            headers=_HEADERS,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["eligibility"]["state"] == "unresolved_review"
        assert body["eligibility"]["reward_eligible"] is False
        assert body["eligibility"]["artifact_sha256"] == _SHA
        assert body["eligibility"]["enforcement"] == "enforce"
        assert body["effective"]["settings"]["enforcement"] == "enforce"
        assert "in_ledger" in body

    async def test_unknown_agent_is_a_404_not_an_empty_verdict(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/agents/{uuid4()}/emission-eligibility", headers=_HEADERS
        )
        assert response.status_code == 404
