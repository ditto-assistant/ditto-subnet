"""Backroom reads of per-agent harness memory (SN-500 step 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.validator import HarnessMemoryTelemetry
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent
from ditto.db.queries.harness_memory import record_harness_memory

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_MIB = 1 << 20


def _install(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _agent(session: AsyncSession, name: str) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="5" + "M" * 47,
            name=name,
            sha256="ab" * 32,
            status=AgentStatus.SCORED,
            screening_policy_version=SCREENING_POLICY_VERSION,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return agent_id


def _telemetry(p50: int, p95: int, peak: int) -> HarnessMemoryTelemetry:
    return HarnessMemoryTelemetry(
        peak_bytes=peak * _MIB,
        p50_bytes=p50 * _MIB,
        p95_bytes=p95 * _MIB,
        samples=100,
        sampled_seconds=200.0,
        limit_bytes=2048 * _MIB,
    )


async def _observe(
    session: AsyncSession,
    agent_id: UUID,
    validator: str,
    telemetry: HarnessMemoryTelemetry,
    *,
    outcome: str = "scored",
    detail: str | None = None,
    issued: datetime | None = None,
) -> None:
    await record_harness_memory(
        session,
        agent_id=agent_id,
        bench_version=18,
        validator_hotkey=validator,
        lease_issued_at=issued or datetime.now(UTC),
        outcome=outcome,  # type: ignore[arg-type]
        telemetry=telemetry,
        failure_reason="sandbox_oom" if detail else None,
        failure_detail=detail,
    )


async def test_reads_require_the_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get("/api/v1/admin/harness-memory")).status_code == 401
    assert (
        await client.get(f"/api/v1/admin/agents/{uuid4()}/harness-memory")
    ).status_code == 401


async def test_rollup_per_agent_version_and_heaviest_first(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    issued = datetime.now(UTC) - timedelta(hours=1)
    async with session_maker() as session, session.begin():
        light = await _agent(session, "light-agent")
        heavy = await _agent(session, "heavy-agent")
        unseen = await _agent(session, "unmeasured-agent")
        await _observe(session, light, "5A", _telemetry(200, 300, 350), issued=issued)
        await _observe(session, light, "5B", _telemetry(220, 340, 400), issued=issued)
        await _observe(session, heavy, "5A", _telemetry(900, 1800, 1990), issued=issued)
        await _observe(
            session,
            heavy,
            "5B",
            _telemetry(1500, 2040, 2048),
            outcome="failed",
            detail="harness_oom",
            issued=issued,
        )
        await _observe(session, heavy, "5C", _telemetry(700, 1500, 1600), issued=issued)
        # A replayed report for the same lease overwrites, never double-counts.
        await _observe(session, heavy, "5C", _telemetry(700, 1600, 1600), issued=issued)

    listing = await client.get("/api/v1/admin/harness-memory", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [row["agent_name"] for row in body["agents"]] == [
        "heavy-agent",
        "light-agent",
    ]
    assert "no memory data" in body["note"]
    heavy_row = body["agents"][0]
    assert heavy_row["runs"] == 3
    assert (heavy_row["scored_runs"], heavy_row["failed_runs"]) == (2, 1)
    assert heavy_row["oom_runs"] == 1
    assert heavy_row["peak_bytes_max"] == 2048 * _MIB
    assert heavy_row["p95_bytes_max"] == 2040 * _MIB
    assert heavy_row["p95_bytes_median"] == 1800 * _MIB
    assert heavy_row["p50_bytes_median"] == 900 * _MIB
    assert heavy_row["limit_bytes_max"] == 2048 * _MIB

    detail = await client.get(
        f"/api/v1/admin/agents/{heavy}/harness-memory", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    runs = detail.json()["runs"]
    assert len(runs) == 3
    assert {run["validator_hotkey"] for run in runs} == {"5A", "5B", "5C"}
    assert (
        next(run for run in runs if run["validator_hotkey"] == "5C")["p95_bytes"]
        == 1600 * _MIB
    )

    # An agent never measured is reported as such, not as light.
    empty = await client.get(
        f"/api/v1/admin/agents/{unseen}/harness-memory", headers=_HEADERS
    )
    assert empty.status_code == 200
    assert empty.json() == {"agent_id": str(unseen), "rollup": None, "runs": []}


async def test_listing_pages_with_offset_and_has_more(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    async with session_maker() as session, session.begin():
        for index, p95 in enumerate((1500, 1200, 900)):
            agent_id = await _agent(session, f"agent-{index}")
            await _observe(session, agent_id, "5A", _telemetry(p95 // 2, p95, p95))

    first = (
        await client.get("/api/v1/admin/harness-memory?limit=2", headers=_HEADERS)
    ).json()
    assert [row["agent_name"] for row in first["agents"]] == ["agent-0", "agent-1"]
    assert (first["offset"], first["has_more"]) == (0, True)

    rest = (
        await client.get(
            "/api/v1/admin/harness-memory?limit=2&offset=2", headers=_HEADERS
        )
    ).json()
    assert [row["agent_name"] for row in rest["agents"]] == ["agent-2"]
    assert (rest["offset"], rest["has_more"]) == (2, False)
