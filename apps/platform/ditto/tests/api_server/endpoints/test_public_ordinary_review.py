"""Source-safe ordinary-review projection on the public pipeline (#2042).

``ordinary_review`` distinguishes active work, capacity wait, infrastructure
backoff, and escalation for a miner's own submission without exposing
operator-only detail (quarantine evidence, reason codes) or any other
miner's data.
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
from ditto.db.models import Agent, ScreeningAttempt, ScreeningQuarantine

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
    created_at: datetime | None = None,
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
                created_at=created_at or datetime.now(UTC),
            )
        )
    return agent_id


async def test_uploaded_agent_reports_capacity_wait(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(
        maker,
        name="capacity-wait",
        status=AgentStatus.UPLOADED,
        created_at=now - timedelta(seconds=45),
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    review = response.json()["ordinary_review"]
    assert review is not None
    assert review["reason"] == "capacity_wait"
    assert review["age_seconds"] >= 45


async def test_claimed_agent_reports_active_work(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(
        maker,
        name="active-work",
        status=AgentStatus.SCREENING,
        created_at=now - timedelta(hours=1),
    )
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=agent_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                started_at=now - timedelta(seconds=20),
                deadline=now + timedelta(hours=1),
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    review = response.json()["ordinary_review"]
    assert review is not None
    assert review["reason"] == "active_work"
    # Reflects the current attempt (~20s), not the agent's full 1h age.
    assert review["age_seconds"] < 120


async def test_failed_agent_reports_infrastructure_backoff(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(
        maker, name="infra-backoff", status=AgentStatus.SCREENING_FAILED
    )
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=agent_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=now - timedelta(minutes=3),
                deadline=now + timedelta(hours=1),
                finished_at=now - timedelta(minutes=2),
                reason_code="source-review-retryable-infra",
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    review = response.json()["ordinary_review"]
    assert review is not None
    assert review["reason"] == "infrastructure_backoff"


async def test_quarantined_agent_reports_escalation_with_no_operator_detail(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_agent(
        maker, name="escalation", status=AgentStatus.QUARANTINED
    )
    attempt_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                status="quarantined",
                started_at=now - timedelta(minutes=10),
                deadline=now + timedelta(hours=1),
            )
        )
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="c" * 64,
                reason_code="agentic-source-review-tripwire",
                finding_digest="d" * 64,
                finding={
                    "artifact_sha256": "e" * 64,
                    "risk_level": "high",
                    "confidence": 0.9,
                    "categories": ["benchmark_emulation"],
                    "summary": "operator-only evidence summary",
                },
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    body = response.json()
    review = body["ordinary_review"]
    assert review is not None
    assert review["reason"] == "escalation"
    # Exactly the four source-safe fields -- no reason_code, no finding, no
    # evidence, no other miner's data anywhere in this projection.
    assert set(review) == {
        "reason",
        "age_seconds",
        "typical_p50_seconds",
        "typical_p95_seconds",
    }
    assert "operator-only evidence summary" not in response.text
    assert "agentic-source-review-tripwire" not in response.text


async def test_resolved_quarantine_reconciliation_gap_reports_no_ordinary_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """A miner sees no active review for a ghost, matching the operator view."""
    now = datetime.now(UTC)
    agent_id = await _seed_agent(
        maker, name="ghost-quarantine", status=AgentStatus.QUARANTINED
    )
    attempt_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                status="quarantined",
                started_at=now - timedelta(hours=2),
                deadline=now + timedelta(hours=1),
            )
        )
        session.add(
            ScreeningQuarantine(
                quarantine_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_hotkey("screener"),
                policy_version=SCREENING_POLICY_VERSION,
                manifest_digest="f" * 64,
                reason_code="source-review-inconclusive",
                status="resolved",
                resolved_at=now - timedelta(hours=1),
                resolved_by="operator@example.com",
                resolution="release",
                resolution_reason="cleared on appeal",
            )
        )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["ordinary_review"] is None


async def test_scored_agent_reports_no_ordinary_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_agent(
        maker, name="already-scored", status=AgentStatus.SCORED
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    assert response.json()["ordinary_review"] is None
