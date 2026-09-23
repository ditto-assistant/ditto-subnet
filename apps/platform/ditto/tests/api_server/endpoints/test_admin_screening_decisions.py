"""Backroom read surface for policy-v13 screening decision records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, ScreeningAttempt, ScreeningQuarantine
from ditto.db.queries.screening_decisions import record_screening_decision
from ditto_screening_protocol import (
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    REVIEW_TIMEOUT_FINALIZER_ACTOR,
    STRICT_TWO_OUTCOME_POLICY_VERSION,
    V2_PLATFORM_VERIFICATION_FAILED,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_agent(maker: async_sessionmaker[AsyncSession]) -> Agent:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        agent = Agent(
            agent_id=agent_id,
            miner_hotkey="5Decided",
            name="decided",
            sha256=agent_id.hex * 2,
            status=AgentStatus.SCREENING_FAILED,
            screening_policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
        session.add(agent)
    return agent


async def test_agent_without_decisions_reads_empty_history_and_published_policy(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    agent = await _seed_agent(session_maker)

    response = await client.get(
        f"/api/v1/admin/screening-decisions/{agent.agent_id}", headers=_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == str(agent.agent_id)
    assert body["agent_status"] == "screening_failed"
    assert body["latest"] is None
    assert body["decisions"] == []
    policy = body["review_timeout_policy"]
    assert policy["artifact_failure_retries"] == 1
    assert policy["provider_failure_retries"] == 2
    assert policy["platform_failure_retries"] == 2
    assert policy["max_verification_window_hours"] == 24
    assert policy["terminal_outcome"] == REVIEW_TIMED_OUT_OUTCOME
    assert policy["ban_on_timeout"] is False
    assert policy["precedent_weight_on_timeout"] is False
    assert policy["automatic_priority_rescreen_on_recovery"] is True
    assert policy["no_fault_retry_grant_on_timeout"] is True
    assert (
        policy["applies_from_policy_version"]
        == PUBLISHED_REVIEW_TIMEOUT_POLICY.applies_from_policy_version
    )
    thresholds = body["review_capacity_thresholds"]
    assert thresholds["max_fail_open_rate"] == 0.05
    assert thresholds["min_healthy_source_review_workers"] >= 1


async def test_recorded_decisions_read_newest_first_with_outcome_counts(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    agent = await _seed_agent(session_maker)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    async with session_maker() as session, session.begin():
        locked = await session.get(Agent, agent.agent_id)
        assert locked is not None
        first = await record_screening_decision(
            session,
            agent=locked,
            outcome=REVIEW_TIMED_OUT_OUTCOME,
            reason_codes=["review-timed-out", V2_PLATFORM_VERIFICATION_FAILED],
            violation_proven=False,
            failure_domain="platform",
            retry_count=2,
            independent_workers=1,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            public_reason="no-fault timeout",
            reviewer=REVIEW_TIMEOUT_FINALIZER_ACTOR,
            decided_at=t0,
            evidence_references=[],
            completed_checks=["archive-sha256"],
            failed_checks=["source-review-inconclusive"],
            limitations=["bounded review exhausted"],
        )
        second = await record_screening_decision(
            session,
            agent=locked,
            outcome="clear",
            reason_codes=[],
            violation_proven=False,
            failure_domain="none",
            retry_count=0,
            independent_workers=0,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            public_reason="operator read the served path",
            reviewer="operator@example.test",
            decided_at=t0 + timedelta(hours=1),
            evidence_references=["src/main.rs:42", "src/lib.rs:7-9"],
            completed_checks=["operator-ath-review"],
            failed_checks=[],
            limitations=[],
            precedent_weight=True,
        )

    response = await client.get(
        f"/api/v1/admin/screening-decisions/{agent.agent_id}", headers=_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert [row["decision_id"] for row in body["decisions"]] == [
        str(second.decision_id),
        str(first.decision_id),
    ]
    latest = body["latest"]
    assert latest["outcome"] == "clear"
    assert latest["evidence_references"] == ["src/main.rs:42", "src/lib.rs:7-9"]
    assert latest["supersedes_decision"] == str(first.decision_id)
    assert latest["precedent_weight"] is True
    assert latest["no_fault"] is False
    assert latest["is_verification_failure"] is False
    timeout = body["decisions"][1]
    assert timeout["outcome"] == REVIEW_TIMED_OUT_OUTCOME
    assert timeout["violation_proven"] is False
    assert timeout["precedent_weight"] is False
    assert timeout["no_fault"] is True
    assert timeout["is_verification_failure"] is True
    assert timeout["failure_domain"] == "platform"
    assert timeout["retry_count"] == 2
    assert timeout["independent_workers"] == 1
    assert timeout["identities"]["submission_uuid"] == str(agent.agent_id)
    assert timeout["identities"]["artifact_sha256"] == agent.sha256
    assert (
        timeout["identities"]["applied_policy_version"]
        == STRICT_TWO_OUTCOME_POLICY_VERSION
    )

    listed = await client.get(
        "/api/v1/admin/screening-decisions?outcome=review_timed_out",
        headers=_HEADERS,
    )
    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["count"] == 1
    assert [row["decision_id"] for row in listed_body["items"]] == [
        str(first.decision_id)
    ]
    assert listed_body["outcome"] == REVIEW_TIMED_OUT_OUTCOME
    assert listed_body["outcome_counts"] == {
        "clear": 1,
        "reject": 0,
        "review_timed_out": 1,
    }


async def test_decision_reads_require_admin(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    response = await client.get(f"/api/v1/admin/screening-decisions/{uuid4()}")
    assert response.status_code in (401, 403)
    response = await client.get("/api/v1/admin/screening-decisions")
    assert response.status_code in (401, 403)


async def test_verification_state_keeps_attempt_and_artifact_deadlines_separate(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    agent = await _seed_agent(session_maker)
    started = datetime.now(UTC) - timedelta(hours=25)
    attempt_id, quarantine_id = uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        locked = await session.get(Agent, agent.agent_id)
        assert locked is not None
        locked.status = AgentStatus.QUARANTINED
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent.agent_id,
                screener_hotkey="5WorkerA",
                policy_version=13,
                status="quarantined",
                started_at=started,
                deadline=started + timedelta(minutes=20),
                finished_at=started + timedelta(minutes=10),
                reason_code="source-review-inconclusive",
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent.agent_id,
                attempt_id=attempt_id,
                screener_hotkey="5WorkerA",
                policy_version=13,
                manifest_digest="a" * 64,
                reason_code="source-review-inconclusive",
                status="active",
                created_at=started,
            )
        )

    url = f"/api/v1/admin/screening-decisions/{agent.agent_id}/verification-state"
    monkeypatch.setenv("DITTO_REVIEW_TIMEOUT_FINALIZER_MODE", "shadow")
    shadow = await client.get(url, headers=_HEADERS)
    assert shadow.status_code == 200
    assert shadow.json()["finalizer_state"] == "not_configured"
    assert shadow.json()["verification_deadline"] is None
    assert shadow.json()["attempt_deadline"] is not None
    assert shadow.json()["mandatory_checks_state"] == "not_recorded"

    monkeypatch.setenv("DITTO_REVIEW_TIMEOUT_FINALIZER_MODE", "enforce")
    enforced = await client.get(url, headers=_HEADERS)
    assert enforced.status_code == 200
    body = enforced.json()
    assert body["finalizer_state"] == "ready"
    assert datetime.fromisoformat(body["verification_deadline"]) == started + timedelta(
        hours=24
    )
    assert body["deadline_provenance"].startswith("shipped_finalizer_default")
    assert body["attempts_recorded"] == 1
    assert body["independent_workers"] == 1
    assert body["automatic_retry_budget"] == 2
    assert body["retries_used"] == 0


async def test_verification_state_unknown_agent_has_no_deadline(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    response = await client.get(
        f"/api/v1/admin/screening-decisions/{uuid4()}/verification-state",
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["finalizer_state"] == "not_configured"
    assert response.json()["verification_deadline"] is None


async def test_verification_state_legacy_and_changed_artifact_are_not_finalized(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    agent = await _seed_agent(session_maker)
    async with session_maker() as session, session.begin():
        locked = await session.get(Agent, agent.agent_id)
        assert locked is not None
        await record_screening_decision(
            session,
            agent=locked,
            outcome="clear",
            reason_codes=[],
            violation_proven=False,
            failure_domain="none",
            retry_count=0,
            independent_workers=0,
            policy_version=13,
            public_reason="source reviewed",
            reviewer="operator@example.test",
            decided_at=datetime.now(UTC),
            evidence_references=["src/main.rs:42"],
            completed_checks=["source-review"],
            failed_checks=[],
            limitations=[],
        )
        locked.sha256 = "f" * 64
        locked.screening_policy_version = 12

    response = await client.get(
        f"/api/v1/admin/screening-decisions/{agent.agent_id}/verification-state",
        headers=_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["policy_version"] == 12
    assert body["decision_matches_artifact"] is False
    assert body["finalizer_state"] == "not_configured"
    assert body["verification_deadline"] is None
    assert body["image_digest"] is None
