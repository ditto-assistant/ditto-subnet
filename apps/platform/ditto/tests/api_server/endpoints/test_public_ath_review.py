"""Source-safe ATH-hold projection on the public pipeline (#2042, slice 2).

``ath_review`` tells a miner whether their held submission is waiting for a
deep-review screener, being worked, parked after an infrastructure failure,
or waiting on an operator. It exposes no hold evidence, no queue class, and
no other miner's data.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, AthReview, ScreeningAttempt

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


async def _seed_hold(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    status: AgentStatus = AgentStatus.ATH_PENDING_REVIEW,
    opened_at: datetime,
    provenance: dict[str, Any],
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
                created_at=opened_at - timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            AthReview(
                review_id=uuid4(),
                agent_id=agent_id,
                status="pending",
                opened_at=opened_at,
                original_reason="private operator-only hold reason",
                original_policy_version=8,
                original_evidence={"secret": "operator-only evidence"},
                algorithm_provenance=provenance,
            )
        )
    return agent_id


async def _pipeline(client: httpx.AsyncClient, agent_id: UUID) -> dict[str, Any]:
    response = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_copy_hold_reports_escalation_without_private_detail(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_hold(
        maker,
        name="copy-hold",
        opened_at=datetime.now(UTC) - timedelta(minutes=30),
        provenance={"review_kind": "copy"},
    )
    _install(app, maker)

    review = (await _pipeline(client, agent_id))["ath_review"]
    assert review is not None
    assert set(review) == {
        "reason",
        "age_seconds",
        "typical_p50_seconds",
        "typical_p95_seconds",
    }
    assert review["reason"] == "escalation"
    assert review["age_seconds"] >= 1800
    assert review["typical_p50_seconds"] is not None
    rendered = str(review)
    assert "operator-only" not in rendered
    assert "copy" not in rendered


async def test_deferred_hold_reports_active_deep_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    agent_id = await _seed_hold(
        maker,
        name="deep-review",
        opened_at=now - timedelta(hours=2),
        provenance={"review_kind": "deferred_source_review"},
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

    review = (await _pipeline(client, agent_id))["ath_review"]
    assert review is not None
    assert review["reason"] == "active_work"
    # The hold clock, not the fresh deep-review attempt.
    assert review["age_seconds"] >= 7000


async def test_stranded_hold_is_not_shown_as_active_review(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    agent_id = await _seed_hold(
        maker,
        name="stranded",
        status=AgentStatus.SCORED,
        opened_at=datetime.now(UTC) - timedelta(days=3),
        provenance={"review_kind": "copy"},
    )
    _install(app, maker)

    assert (await _pipeline(client, agent_id))["ath_review"] is None
