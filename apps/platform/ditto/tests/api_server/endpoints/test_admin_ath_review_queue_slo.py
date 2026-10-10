"""Backroom read of the ATH-hold and copy-review queue-age SLO endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.ath_review_queue_slo_config import (
    COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV,
    AthReviewQueueSloConfig,
    parse_ath_review_queue_slo_config_from_env,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.errors import ApiServerConfigError
from ditto.db.models import Agent, AgentStatus, AthReview
from ditto.db.queries.ath_review_queue_slo import QueueAgeThresholds

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_URL = "/api/v1/admin/ath-review-queue-slo"
_STATS_KEYS = {
    "queue_class",
    "backlog_count",
    "active_work_count",
    "capacity_wait_count",
    "infrastructure_backoff_count",
    "escalation_count",
    "p50_age_seconds",
    "p95_age_seconds",
    "oldest_age_seconds",
    "oldest_agent_id",
    "oldest_reason",
    "throughput_window_hours",
    "throughput_completed_count",
    "throughput_per_hour",
    "max_actionable_age_threshold_seconds",
    "overdue_count",
    "p95_age_threshold_seconds",
    "p95_exceeds_threshold",
}


def _install(
    app: FastAPI, *, slo_config: AthReviewQueueSloConfig | None = None
) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token=_ADMIN_TOKEN,
        ath_review_queue_slo=slo_config or AthReviewQueueSloConfig(),
    )


def _install_session(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_copy_hold(
    session_maker: async_sessionmaker[AsyncSession], *, opened_at: datetime
) -> None:
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5" + "F" * 47,
                name="agent",
                sha256="a" * 64,
                status=AgentStatus.ATH_PENDING_REVIEW,
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
                original_reason="near-copy signal",
                original_policy_version=8,
                original_evidence={},
                algorithm_provenance={"review_kind": "copy"},
            )
        )


class TestConfig:
    def test_thresholds_default_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, raising=False)
        config = parse_ath_review_queue_slo_config_from_env()
        assert config.copy.max_actionable_age_seconds is None
        assert config.ath.p95_age_seconds is None

    def test_parses_a_positive_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, "86400")
        config = parse_ath_review_queue_slo_config_from_env()
        assert config.copy.max_actionable_age_seconds == 86400

    @pytest.mark.parametrize("raw", ["0", "-5", "soon"])
    def test_rejects_unusable_threshold_at_boot(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(COPY_REVIEW_QUEUE_MAX_AGE_THRESHOLD_ENV, raw)
        with pytest.raises(ApiServerConfigError):
            parse_ath_review_queue_slo_config_from_env()


@pytest.mark.asyncio
class TestAuth:
    async def test_read_requires_the_admin_token(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        _install(app)
        assert (await client.get(_URL)).status_code == 401


@pytest.mark.asyncio
class TestReadsTheQueue:
    async def test_reports_shape_and_unset_threshold_as_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app)
        _install_session(app, session_maker)
        await _seed_copy_hold(session_maker, opened_at=datetime.now(UTC))

        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "generated_at",
            "ath",
            "copy_review",
            "classes",
            "stranded_terminal_ghost_count",
            "stranded_hold_ghost_count",
            "held_without_review_ghost_count",
            "ghost_count",
        }
        assert set(body["ath"]) == _STATS_KEYS
        assert body["ath"]["queue_class"] is None
        assert body["copy_review"]["queue_class"] == "copy"
        assert [entry["queue_class"] for entry in body["classes"]] == [
            "copy",
            "benchmark_overfit",
            "deferred_source_review",
            "integrity_double_check",
            "anomalous_score",
        ]
        assert body["ath"]["backlog_count"] == 1
        assert body["copy_review"]["escalation_count"] == 1
        assert body["ath"]["overdue_count"] is None
        assert body["ath"]["p95_exceeds_threshold"] is None
        assert body["copy_review"]["overdue_count"] is None

    async def test_configured_thresholds_report_real_overdue_counts(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(
            app,
            slo_config=AthReviewQueueSloConfig(
                ath=QueueAgeThresholds(max_actionable_age_seconds=7200),
                copy=QueueAgeThresholds(max_actionable_age_seconds=60),
            ),
        )
        _install_session(app, session_maker)
        await _seed_copy_hold(
            session_maker, opened_at=datetime.now(UTC) - timedelta(hours=1)
        )

        response = await client.get(_URL, headers=_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ath"]["max_actionable_age_threshold_seconds"] == 7200
        assert body["ath"]["overdue_count"] == 0
        assert body["copy_review"]["max_actionable_age_threshold_seconds"] == 60
        assert body["copy_review"]["overdue_count"] == 1
        assert body["copy_review"]["oldest_reason"] == "escalation"
