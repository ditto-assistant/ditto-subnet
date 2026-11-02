"""A parked screening failure is actionable, not historical or queued work."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, ScreeningAttempt, ScreeningRetryOverride
from ditto.tests.api_server.endpoints.test_public import (
    _MINER_A,
    _install_db,
    _seed_agent,
)


@pytest.mark.parametrize("authorized", [False, True])
async def test_parked_screening_status_is_consistent_across_public_surfaces(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    authorized: bool,
) -> None:
    agent_id = UUID(
        await _seed_agent(
            session_maker,
            miner=_MINER_A,
            name="recent interrupted submission",
            status=AgentStatus.SCREENING_FAILED,
        )
    )
    now = datetime.now(UTC)
    attempt_id = uuid4()
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_MINER_A,
                policy_version=13,
                status="failed",
                started_at=now - timedelta(minutes=10),
                deadline=now + timedelta(minutes=10),
                finished_at=now - timedelta(minutes=1),
                reason_code="worker-result-processing-failed",
            )
        )
        if authorized:
            session.add(
                ScreeningRetryOverride(
                    override_id=uuid4(),
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    artifact_sha256=agent.sha256,
                    expected_score_count=0,
                    reason="retry after worker fix",
                    actor="test",
                )
            )
    _install_db(app, session_maker)
    expected = "waiting_screening" if authorized else "screening_failed"

    activity_response = await client.get(f"/api/v1/public/activity?status={expected}")
    assert activity_response.status_code == 200
    activity = activity_response.json()
    assert activity["status_counts"].get("not_queued", 0) == 0
    assert activity["status_counts"].get("waiting_screening", 0) == int(authorized)
    assert activity["entries"][0]["status"] == expected

    operations = (await client.get("/api/v1/public/operations")).json()
    entry = next(
        row
        for row in operations["activity"]["entries"]
        if row["agent_id"] == str(agent_id)
    )
    assert entry["status"] == expected
    for surface in ("summary", "pipeline"):
        response = await client.get(f"/api/v1/public/agent/{agent_id}/{surface}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == expected
        if surface == "pipeline":
            assert body["admission_retry"]["state"] == (
                "retry_queued" if authorized else "parked"
            )
