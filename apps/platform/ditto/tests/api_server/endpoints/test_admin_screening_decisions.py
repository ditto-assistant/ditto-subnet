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
from ditto.db.models import Agent
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
