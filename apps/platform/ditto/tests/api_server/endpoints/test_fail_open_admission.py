"""Fail-open court clears hold the agent instead of feeding the eligible ledger."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.fail_open_admission import (
    BACKFILL_ACTOR,
    FAIL_OPEN_REVIEW_REASON,
    adjudication_is_fail_open,
    evidence_marks_fail_open,
    hold_fail_open_admission,
    latest_fail_open_admission,
)
from ditto.db.models import (
    Agent,
    AthReview,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.scores import list_eligible_ledger

_TOKEN = "admin-token-fail-open-tests"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_URL = "/api/v1/admin/copy-reviews/backfill-fail-open"
_T0 = datetime(2026, 9, 6, 15, tzinfo=UTC)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _adjudication_evidence(
    *, clear_clause: str | None, summary: str = "cleared"
) -> list[dict[str, object]]:
    return [
        {
            "module_id": "adjudication",
            "code": "adjudicated-source-review-clear",
            "summary": summary,
            "digest": "ab" * 32,
            "clear_clause": clear_clause,
            "escalation_code": None,
        }
    ]


def _quarantine(
    agent_id: UUID, *, evidence: list[dict[str, object]], created_at: datetime
) -> list[ScreeningAttempt | ScreeningQuarantine]:
    """One passed attempt plus the adjudicated court record it produced."""
    attempt_id = uuid4()
    return [
        ScreeningAttempt(
            attempt_id=attempt_id,
            agent_id=agent_id,
            screener_hotkey="5Screener",
            policy_version=12,
            status="passed",
            started_at=created_at - timedelta(minutes=6),
            deadline=created_at + timedelta(minutes=10),
            finished_at=created_at,
            reason_code="adjudicated-source-review-clear",
        ),
        ScreeningQuarantine(
            quarantine_id=uuid4(),
            agent_id=agent_id,
            attempt_id=attempt_id,
            screener_hotkey="5Screener",
            policy_version=12,
            manifest_digest="cd" * 32,
            reason_code="adjudicated-source-review-clear",
            evidence=evidence,
            status="resolved",
            created_at=created_at,
            resolved_at=created_at,
        ),
    ]


def _agent(agent_id: UUID, name: str, status: AgentStatus) -> Agent:
    return Agent(
        agent_id=agent_id,
        miner_hotkey=f"5{name}",
        name=name,
        version=1,
        sha256=agent_id.hex * 2,
        status=status,
        screening_policy_version=12,
        created_at=_T0 - timedelta(hours=1),
    )


def test_fail_open_predicates_read_the_clause_and_the_legacy_prose() -> None:
    assert adjudication_is_fail_open(
        decision="clear", clear_clause="no_proven_breach_before_deadline", reason=""
    )
    assert adjudication_is_fail_open(
        decision="clear",
        clear_clause=None,
        reason=(
            "Automated adjudication ended (adjudicator-failed) without a verified "
            "policy breach after considering 0 persisted review notes; cleared "
            "under the no-proven-breach-before-deadline rule"
        ),
    )
    assert not adjudication_is_fail_open(
        decision="clear", clear_clause="untrusted_candidate_channel", reason="ok"
    )
    assert not adjudication_is_fail_open(
        decision="reject", clear_clause=None, reason="no-proven-breach-before-deadline"
    )
    assert evidence_marks_fail_open(
        _adjudication_evidence(clear_clause="no_proven_breach_before_deadline")
    )
    assert evidence_marks_fail_open(
        _adjudication_evidence(
            clear_clause=None,
            summary="... cleared under the no-proven-breach-before-deadline rule",
        )
    )
    assert not evidence_marks_fail_open(
        _adjudication_evidence(clear_clause="shape_only_validation")
    )
    assert not evidence_marks_fail_open(None)


async def test_backfill_holds_fail_open_admissions_once_and_skips_operator_clears(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    fail_open_id, certified_id, cleared_id, live_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                _agent(fail_open_id, "failopen", AgentStatus.SCORED),
                _agent(certified_id, "certified", AgentStatus.SCORED),
                _agent(cleared_id, "cleared", AgentStatus.SCORED),
                _agent(live_id, "livefailopen", AgentStatus.LIVE),
                *_quarantine(
                    fail_open_id,
                    evidence=_adjudication_evidence(
                        clear_clause="no_proven_breach_before_deadline"
                    ),
                    created_at=_T0,
                ),
                *_quarantine(
                    certified_id,
                    evidence=_adjudication_evidence(
                        clear_clause="untrusted_candidate_channel"
                    ),
                    created_at=_T0,
                ),
                *_quarantine(
                    cleared_id,
                    evidence=_adjudication_evidence(
                        clear_clause="no_proven_breach_before_deadline"
                    ),
                    created_at=_T0,
                ),
                *_quarantine(
                    live_id,
                    evidence=_adjudication_evidence(
                        clear_clause=None,
                        summary=(
                            "cleared under the no-proven-breach-before-deadline rule"
                        ),
                    ),
                    created_at=_T0,
                ),
                # An operator already read `cleared` after its admission: skip it.
                AthReview(
                    review_id=uuid4(),
                    agent_id=cleared_id,
                    status="resolved",
                    opened_at=_T0 + timedelta(hours=1),
                    resolved_at=_T0 + timedelta(hours=2),
                    resolved_by="peyton@example.test",
                    resolution="clear",
                    resolution_reason="read the served path; no breach",
                    original_reason="manual review",
                    original_policy_version=12,
                    original_evidence={"previous_status": "scored"},
                    algorithm_provenance={"snapshot": "manual"},
                ),
            ]
        )

    dry = await client.post(_URL, json={"dry_run": True}, headers=_HEADERS)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True and body["opened"] == 0
    actions = {item["agent_id"]: item["action"] for item in body["items"]}
    assert actions == {
        str(fail_open_id): "would_open",
        str(live_id): "would_open",
        str(cleared_id): "skipped_cleared",
    }
    async with session_maker() as session:
        untouched = await session.get(Agent, fail_open_id)
        assert untouched is not None and untouched.status == AgentStatus.SCORED

    applied = await client.post(_URL, json={"dry_run": False}, headers=_HEADERS)
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["opened"] == 2 and body["skipped"] == 1
    async with session_maker() as session:
        held = await session.get(Agent, fail_open_id)
        live = await session.get(Agent, live_id)
        certified = await session.get(Agent, certified_id)
        assert held is not None and held.status == AgentStatus.ATH_PENDING_REVIEW
        assert held.review_reason == FAIL_OPEN_REVIEW_REASON
        assert live is not None and live.status == AgentStatus.ATH_PENDING_REVIEW
        assert certified is not None and certified.status == AgentStatus.SCORED
        review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == fail_open_id)
        )
        assert review is not None and review.status == "pending"
        assert review.original_evidence["previous_status"] == "scored"
        assert review.algorithm_provenance["review_kind"] == "deferred_source_review"
        assert review.algorithm_provenance["opened_by"] == BACKFILL_ACTOR
        assert review.algorithm_provenance["backfilled"] is True
        live_review = await session.scalar(
            select(AthReview).where(AthReview.agent_id == live_id)
        )
        assert live_review is not None
        assert live_review.original_evidence["previous_status"] == "live"
        assert review.original_evidence["admitting_attempt_id"]
        assert review.original_reason == FAIL_OPEN_REVIEW_REASON
        ledger_ids = {row.agent_id for row in await list_eligible_ledger(session)}
        assert fail_open_id not in ledger_ids and live_id not in ledger_ids

    again = await client.post(_URL, json={"dry_run": False}, headers=_HEADERS)
    assert again.status_code == 200, again.text
    assert again.json()["opened"] == 0
    async with session_maker() as session:
        reviews = (
            await session.scalars(
                select(AthReview).where(AthReview.agent_id == fail_open_id)
            )
        ).all()
        assert len(reviews) == 1, "the backfill must be idempotent"


async def test_newer_certified_clear_supersedes_an_older_fail_open_row(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                _agent(agent_id, "rescreened", AgentStatus.SCORED),
                *_quarantine(
                    agent_id,
                    evidence=_adjudication_evidence(
                        clear_clause="no_proven_breach_before_deadline"
                    ),
                    created_at=_T0,
                ),
                *_quarantine(
                    agent_id,
                    evidence=_adjudication_evidence(
                        clear_clause="untrusted_candidate_channel"
                    ),
                    created_at=_T0 + timedelta(hours=3),
                ),
            ]
        )
    async with session_maker() as session:
        assert await latest_fail_open_admission(session, agent_id=agent_id) is None


async def test_hold_helper_refuses_agents_that_are_not_eligible(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(_agent(agent_id, "evaluating", AgentStatus.EVALUATING))
        session.add_all(
            _quarantine(
                agent_id,
                evidence=_adjudication_evidence(
                    clear_clause="no_proven_breach_before_deadline"
                ),
                created_at=_T0,
            )
        )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        admission = await latest_fail_open_admission(session, agent_id=agent_id)
        assert agent is not None and admission is not None
        # Not yet scored: the score-finalization hook holds it later, not now.
        assert (
            await hold_fail_open_admission(
                session,
                agent,
                admission=admission,
                now=datetime.now(UTC),
                actor="tests",
                source="unit",
                score_count=0,
            )
            is None
        )
        assert agent.status == AgentStatus.EVALUATING
