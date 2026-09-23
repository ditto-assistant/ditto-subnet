"""Backroom read of the ordinary source-review queue-age SLO endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.source_review_queue_slo_config import SourceReviewQueueSloConfig
from ditto.db.models import Agent, AgentStatus, ScreeningAttempt

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/source-review-queue-slo"


def _install(
    app: FastAPI, *, slo_config: SourceReviewQueueSloConfig | None = None
) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        source_review_queue_slo=slo_config or SourceReviewQueueSloConfig(),
    )


def _install_session(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


class TestAuth:
    async def test_read_requires_the_admin_token(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        _install(app)
        assert (await client.get(_URL)).status_code == 401


class TestReadsTheQueue:
    async def test_reports_shape_and_unset_threshold_as_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app)
        _install_session(app, session_maker)
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5" + "F" * 47,
                    name="agent",
                    sha256="a" * 64,
                    status=AgentStatus.SCREENING,
                    created_at=now,
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey="5" + "F" * 47,
                    policy_version=13,
                    status="running",
                    started_at=now,
                    deadline=now + timedelta(hours=2),
                )
            )

        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "generated_at",
            "backlog_count",
            "active_work_count",
            "capacity_wait_count",
            "infrastructure_backoff_count",
            "escalation_count",
            "p50_age_seconds",
            "p95_age_seconds",
            "oldest_age_seconds",
            "throughput_window_hours",
            "throughput_completed_count",
            "throughput_per_hour",
            "stale_running_ghost_count",
            "resolved_quarantine_ghost_count",
            "ghost_count",
            "max_actionable_age_threshold_seconds",
            "overdue_count",
            "p95_age_threshold_seconds",
            "p95_exceeds_threshold",
        }
        assert body["backlog_count"] == 1
        assert body["active_work_count"] == 1
        assert body["max_actionable_age_threshold_seconds"] is None
        assert body["overdue_count"] is None
        assert body["p95_exceeds_threshold"] is None

    async def test_configured_threshold_reports_a_real_overdue_count(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(
            app,
            slo_config=SourceReviewQueueSloConfig(
                max_actionable_age_threshold_seconds=60
            ),
        )
        _install_session(app, session_maker)
        now = datetime.now(UTC)
        agent_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5" + "F" * 47,
                    name="agent",
                    sha256="a" * 64,
                    status=AgentStatus.UPLOADED,
                    created_at=now - timedelta(hours=1),
                )
            )

        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["max_actionable_age_threshold_seconds"] == 60
        assert body["overdue_count"] == 1
