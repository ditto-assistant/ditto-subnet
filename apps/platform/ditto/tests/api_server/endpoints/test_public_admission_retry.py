"""Honest fail-once admission state on the public pipeline (issue #1215).

``admission_retry`` distinguishes parked provider failures, stuck Ditto
infrastructure, and guarded retries. Only a Docker build infrastructure
failure promises (and schedules) an automatic retry.
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
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    ScreeningAttempt,
    ScreeningRetryOverride,
    ValidatorQueueWithdrawal,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.screening_infra_retry import (
    INFRA_AUTO_RETRY_MAX_STREAK,
    infra_retry_delay,
)

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


async def test_docker_build_infrastructure_reports_the_scheduled_automatic_retry(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="infra-auto", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    finished_at = now - timedelta(minutes=1)
    attempt_id = await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=finished_at,
        deadline=now + timedelta(minutes=60),
        reason_code="docker-build-infrastructure",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry["state"] == "retry_queued"
    assert retry["last_failure_infrastructure"] is True
    assert datetime.fromisoformat(retry["next_retry_at"]) == (
        finished_at + infra_retry_delay(1, attempt_id)
    )


async def test_capped_infrastructure_failure_reports_stuck(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="infra-capped", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    for index in range(INFRA_AUTO_RETRY_MAX_STREAK):
        await _seed_failed_attempt(
            maker,
            agent_id=agent_id,
            finished_at=now - timedelta(hours=5) + timedelta(minutes=30 * index),
            deadline=now + timedelta(minutes=60),
            reason_code="docker-build-infrastructure",
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry["state"] == "stuck"
    assert retry["next_retry_at"] is None
    assert retry["last_failure_infrastructure"] is True


async def test_public_endpoint_does_not_run_the_fleet_history_query(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ditto.db.queries import screening_infra_retry as module

    def forbidden(_cutoff: datetime):  # noqa: ANN202
        raise AssertionError("public endpoint must not scan fleet history")

    monkeypatch.setattr(module, "failing_agents_query", forbidden)
    agent_id = await _seed_agent(
        maker, name="infra-cheap", status=AgentStatus.SCREENING_FAILED
    )
    now = datetime.now(UTC)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="docker-build-infrastructure",
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["admission_retry"]["state"] == "retry_queued"


async def _seed_infra_failure(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> None:
    now = datetime.now(UTC)
    await _seed_failed_attempt(
        maker,
        agent_id=agent_id,
        finished_at=now - timedelta(minutes=1),
        deadline=now + timedelta(minutes=60),
        reason_code="docker-build-infrastructure",
    )


async def test_withdrawn_agent_is_not_promised_an_automatic_retry(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="infra-withdrawn", status=AgentStatus.SCREENING_FAILED
    )
    await _seed_infra_failure(maker, agent_id)
    async with maker() as session, session.begin():
        session.add(
            ValidatorQueueWithdrawal(
                withdrawal_id=uuid4(),
                agent_id=agent_id,
                bench_version=await active_bench_version(session),
                actor="operator@example.com",
                reason="withdrawn",
                expected_snapshot="x",
                score_count=0,
                ticket_snapshot=[],
                created_at=datetime.now(UTC),
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    retry = response.json()["admission_retry"]
    assert retry["state"] == "stuck"
    assert retry["next_retry_at"] is None


async def test_old_era_agent_is_not_promised_an_automatic_retry(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="infra-old-era", status=AgentStatus.SCREENING_FAILED
    )
    await _seed_infra_failure(maker, agent_id)
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        # An activated rollout newer than the agent: it is from a previous era.
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.created_at = now - timedelta(hours=2)
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=6,
                desired_version=7,
                status="activated",
                cohort_size=5,
                created_at=now - timedelta(hours=1),
                activated_at=now,
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["admission_retry"]["state"] == "stuck"


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
