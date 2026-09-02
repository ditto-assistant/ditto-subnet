"""Honest fail-once admission state on the public pipeline (issue #1215).

``admission_retry`` distinguishes parked provider failures, stuck Ditto
infrastructure, and guarded retries without promising automatic retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, ScreeningAttempt, ScreeningRetryOverride

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _hotkey(name: str) -> str:
    digest = sha256(name.encode()).digest()
    body = "".join(_BASE58[byte % len(_BASE58)] for byte in digest)
    return ("5" + (body * 2))[:48]


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    status: AgentStatus,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_hotkey(name),
                name=name,
                sha256=sha256(name.encode()).hexdigest(),
                status=status,
            )
        )
    return agent_id


async def _seed_failed_attempt(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    finished_at: datetime,
    deadline: datetime,
    reason_code: str,
) -> UUID:
    attempt_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=finished_at - timedelta(minutes=15),
                deadline=deadline,
                finished_at=finished_at,
                reason_code=reason_code,
                public_reason="Screening was interrupted; retry scheduled",
            )
        )
    return attempt_id


async def test_source_review_failure_reports_parked_without_retry_time(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="retry-visible", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    finished_at = now - timedelta(minutes=3)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=finished_at,
        deadline=now + timedelta(minutes=40),
        reason_code="source-review-retryable-infra",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry is not None
    assert retry["state"] == "parked"
    assert retry["attempt_count"] == 1
    assert retry["last_failure_infrastructure"] is True
    assert retry["next_retry_at"] is None


async def test_operator_override_reports_immediate_eligibility(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="retry-waived", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    attempt_id = await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="cloudrun-build-unavailable",
    )
    async with maker() as session, session.begin():
        session.add(
            ScreeningRetryOverride(
                override_id=uuid4(),
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=sha256(b"retry-waived").hexdigest(),
                expected_score_count=0,
                reason="operator waiver",
                actor="test-operator",
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry is not None
    assert retry["state"] == "retry_queued"
    assert retry["next_retry_at"] is None


async def test_orphaned_screener_lease_reports_stuck(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="lease-orphaned", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="worker-lease-orphaned",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry["state"] == "stuck"
    assert retry["last_failure_infrastructure"] is True
    assert retry["next_retry_at"] is None


async def test_ditto_infrastructure_failure_reports_stuck(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="infra-stuck", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="cloudrun-build-unavailable",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry["state"] == "stuck"
    assert retry["last_failure_infrastructure"] is True
    assert retry["next_retry_at"] is None


async def test_miner_docker_build_rejection_is_not_retryable_admission(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="miner-build-failed", status=AgentStatus.REJECTED
    )
    now = datetime.now(UTC)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="docker-build",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["admission_retry"] is None


async def test_running_and_terminal_states(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    running = await _seed_agent(
        maker, name="retry-running", status=AgentStatus.SCREENING
    )
    rejected = await _seed_agent(
        maker, name="retry-rejected", status=AgentStatus.REJECTED
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{running}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry is not None
    assert retry["state"] == "running"
    assert retry["next_retry_at"] is None

    response = await client.get(f"/api/v1/public/agent/{rejected}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["admission_retry"] is None
