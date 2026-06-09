"""Integration tests for the retrieval endpoints.

Exercises the real ``api_server`` lifespan against a real Postgres so
the SQLAlchemy query path, the FastAPI router, and the envelope handlers
all run together. Reads only: no chain calls, no S3, no payment. Agents
are seeded by direct ``session_maker`` INSERT rather than walking the
upload-ceremony flow.

Run via ``make test-integration`` (excluded from the default suite).
Requires ``docker compose up`` for postgres + the minio sidecar that the
api_server lifespan opens at boot.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from ditto.api_server import create_api_server, parse_api_server_config_from_env
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_AGENT_NOT_FOUND,
    ERROR_CODE_HOTKEY_AGENT_NOT_FOUND,
)
from ditto.db.models import Agent, AgentStatus

pytestmark = pytest.mark.integration

_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


@pytest.fixture(scope="module", autouse=True)
def _alembic_upgrade_head() -> None:
    """Apply migrations once per module against the real database."""
    env = os.environ.copy()
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
async def _truncate_between_tests() -> AsyncIterator[None]:
    """Wipe ``agents`` before each test so PK + UNIQUE constraints don't
    bleed across function boundaries."""
    from ditto.db import create_db_engine

    engine = create_db_engine()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE evaluation_payments CASCADE"))
        await conn.execute(text("TRUNCATE TABLE agents CASCADE"))
    await engine.dispose()
    yield


async def _seed_agent(
    app: FastAPI,
    *,
    agent_id: UUID | None = None,
    miner_hotkey: str = _HOTKEY,
    name: str = "smoke-agent",
    sha256: str | None = None,
    status: AgentStatus = AgentStatus.UPLOADED,
    created_at: datetime | None = None,
) -> Agent:
    """Insert one agent row via the lifespan-opened ``session_maker``."""
    session_maker = app.state.session_maker
    row = Agent(
        agent_id=agent_id or uuid4(),
        miner_hotkey=miner_hotkey,
        name=name,
        sha256=sha256 or ("deadbeef" * 8),
        status=status,
    )
    if created_at is not None:
        row.created_at = created_at
    async with session_maker() as session, session.begin():
        session.add(row)
    return row


class TestRetrievalIntegration:
    async def test_agent_by_hotkey_happy_path(self) -> None:
        config = parse_api_server_config_from_env(commit_hash="integration-test")
        app = create_api_server(config)
        async with app.router.lifespan_context(app):
            seeded = await _seed_agent(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={_HOTKEY}"
                )

            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"

            body = response.json()
            assert body["agent_id"] == str(seeded.agent_id)
            assert body["miner_hotkey"] == _HOTKEY
            assert body["name"] == seeded.name
            assert body["sha256"] == seeded.sha256
            assert body["status"] == AgentStatus.UPLOADED.value
            # ip_address regression guard at the HTTP boundary.
            assert "ip_address" not in body

    async def test_agent_by_hotkey_returns_latest(self) -> None:
        """Three rows for the same hotkey, varied ``created_at``. The
        endpoint must return the most recent row at the real-PG level."""
        config = parse_api_server_config_from_env(commit_hash="integration-test")
        app = create_api_server(config)
        async with app.router.lifespan_context(app):
            now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
            await _seed_agent(app, created_at=now - timedelta(days=2))
            await _seed_agent(app, created_at=now - timedelta(days=1))
            latest = await _seed_agent(app, created_at=now)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={_HOTKEY}"
                )

            assert response.status_code == 200, response.text
            assert response.json()["agent_id"] == str(latest.agent_id)

    async def test_agent_by_hotkey_404(self) -> None:
        config = parse_api_server_config_from_env(commit_hash="integration-test")
        app = create_api_server(config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={_HOTKEY}"
                )

            assert response.status_code == 404
            assert response.json()["error_code"] == ERROR_CODE_HOTKEY_AGENT_NOT_FOUND

    async def test_agent_status_happy_path(self) -> None:
        config = parse_api_server_config_from_env(commit_hash="integration-test")
        app = create_api_server(config)
        async with app.router.lifespan_context(app):
            seeded = await _seed_agent(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/v1/retrieval/agent/{seeded.agent_id}/status"
                )

            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"

            body = response.json()
            assert set(body.keys()) == {"agent_id", "status"}
            assert body["agent_id"] == str(seeded.agent_id)
            assert body["status"] == AgentStatus.UPLOADED.value

    async def test_agent_status_404(self) -> None:
        config = parse_api_server_config_from_env(commit_hash="integration-test")
        app = create_api_server(config)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.get(f"/api/v1/retrieval/agent/{uuid4()}/status")

            assert response.status_code == 404
            assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_FOUND
