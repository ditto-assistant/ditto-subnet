from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import AgentStatus, ScreeningAttempt
from ditto.tests.api_server.endpoints.test_miner_logs import _login
from ditto.tests.api_server.endpoints.test_validator import (
    _install_chain,
    _install_db,
    _seed_agent,
)


@pytest.mark.asyncio
async def test_owner_gets_complete_private_screening_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = await _seed_agent(
        session_maker,
        status=AgentStatus.SCREENING_FAILED,
        miner_hotkey=miner.ss58_address,
    )
    now = datetime.now(UTC)
    attempt_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=miner.ss58_address,
                policy_version=5,
                status="failed",
                started_at=now - timedelta(minutes=2),
                deadline=now + timedelta(minutes=43),
                finished_at=now,
                public_reason="artifact Docker image did not build",
                reason_code="docker-build",
                failure_provider="gcp",
                failure_lane="kaniko",
                private_failure_detail="DITTO_SUBMISSION_BUILD_FAILED=KANIKO",
                private_failure_log_tail="missing crates/harness/Cargo.toml",
                failure_captured_at=now,
            )
        )
    _install_db(app, session_maker)
    _install_chain(app)
    token = await _login(client, keypair=miner)

    response = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-feedback",
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    failure = response.json()["attempts"][0]
    assert failure["attempt_id"] == str(attempt_id)
    assert failure["reason_code"] == "docker-build"
    assert failure["provider"] == "gcp"
    assert failure["lane"] == "kaniko"
    assert failure["detail"] == "DITTO_SUBMISSION_BUILD_FAILED=KANIKO"
    assert failure["log_tail"] == "missing crates/harness/Cargo.toml"


@pytest.mark.asyncio
async def test_other_miner_cannot_read_screening_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    owner = bittensor.Keypair.create_from_uri("//Alice")
    attacker = bittensor.Keypair.create_from_uri("//Bob")
    agent_id = await _seed_agent(
        session_maker,
        status=AgentStatus.SCREENING_FAILED,
        miner_hotkey=owner.ss58_address,
    )
    _install_db(app, session_maker)
    _install_chain(app)
    token = await _login(client, keypair=attacker)

    response = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-feedback",
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_owner_reads_private_screening_failure_through_miner_mcp(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The signed-in Miner MCP exposes the same owner-only failure record."""
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = await _seed_agent(
        session_maker,
        status=AgentStatus.SCREENING_FAILED,
        miner_hotkey=miner.ss58_address,
    )
    now = datetime.now(UTC)
    attempt_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=miner.ss58_address,
                policy_version=10,
                status="rejected",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
                public_reason="artifact Docker image did not build",
                reason_code="docker-build",
                failure_provider="hetzner",
                failure_lane="buildkit",
                private_failure_detail="missing vendor/harness/Cargo.toml",
                private_failure_log_tail=(
                    "error: No such file or directory (os error 2)"
                ),
                failure_captured_at=now,
            )
        )
    _install_db(app, session_maker)
    _install_chain(app)
    token = await _login(client, keypair=miner)

    response = await client.post(
        "/mcp",
        headers={"authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_my_screening_feedback",
                "arguments": {"agent_id": str(agent_id)},
            },
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False
    feedback = json.loads(result["content"][0]["text"])
    failure = feedback["attempts"][0]
    assert failure["attempt_id"] == str(attempt_id)
    assert failure["provider"] == "hetzner"
    assert failure["lane"] == "buildkit"
    assert failure["detail"] == "missing vendor/harness/Cargo.toml"
