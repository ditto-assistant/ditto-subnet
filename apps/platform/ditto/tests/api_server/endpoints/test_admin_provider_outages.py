"""GET /admin/provider-outages: circuit rows plus the sweep's runs, end to end."""

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
from ditto.db.models import ProviderOutageCircuit
from ditto.db.queries.outage_sweep import sweep_outage_voids
from ditto.tests.db.queries.test_outage_sweep import (
    BAD_SEED,
    HEO,
    OUTAGE_END,
    OUTAGE_START,
    SWEEP_AT,
    V,
    seed_outage_fixture,
)

pytestmark = pytest.mark.asyncio
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _install(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def test_reads_circuit_and_sweep_runs_with_paging(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    _install(app, session_maker)
    await seed_outage_fixture(session)
    now = datetime.now(UTC)
    async with session_maker() as write, write.begin():
        write.add(
            ProviderOutageCircuit(
                provider="openrouter",
                state="open",
                epoch=uuid4(),
                opened_at=now,
                retry_at=now + timedelta(minutes=2),
                last_failure_at=now,
                failure_count=40,
                last_status=402,
                last_error_code="upstream_http_402",
                updated_at=now,
            )
        )
    for at in (SWEEP_AT, SWEEP_AT + timedelta(minutes=5)):
        async with session_maker() as write, write.begin():
            assert await sweep_outage_voids(write, now=at, bench_version=V)

    unauthorized = await client.get("/api/v1/admin/provider-outages")
    assert unauthorized.status_code in {401, 403}

    response = await client.get("/api/v1/admin/provider-outages", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    (circuit,) = body["circuits"]
    assert circuit["state"] == "open"
    assert circuit["last_status"] == 402
    assert circuit["last_error_code"] == "upstream_http_402"
    assert circuit["failure_count"] == 40
    assert circuit["outage_active"] is True
    assert body["sweep_policy"]["batch_limit"] == 200
    assert body["sweep_policy"]["interval_seconds"] == 300
    assert body["sweep_policy"]["first_lookback_seconds"] == 6 * 3600
    assert "upstream_http_402" in body["sweep_policy"]["outage_failure_codes"]
    assert body["runs_total"] == 2
    newest, oldest = body["runs"]
    assert newest["examined_count"] == 0
    assert oldest["voided_count"] == 1
    (void,) = oldest["voids"]
    assert (void["validator_hotkey"], void["seed"]) == (HEO, str(BAD_SEED))
    assert datetime.fromisoformat(void["outage_started_at"]) == OUTAGE_START
    assert datetime.fromisoformat(void["outage_ended_at"]) == OUTAGE_END
    assert oldest["outage_windows"][0]["sample"] == "confirmation"
    (flag,) = oldest["canonical_flagged"]
    assert flag["run_id"] == "canon-heo"

    findings = await client.get(
        "/api/v1/admin/provider-outages",
        params={"with_findings": "true", "limit": 1},
        headers=_HEADERS,
    )
    assert findings.status_code == 200, findings.text
    page = findings.json()
    assert page["runs_total"] == 1
    assert [run["sweep_id"] for run in page["runs"]] == [oldest["sweep_id"]]

    paged = await client.get(
        "/api/v1/admin/provider-outages",
        params={"limit": 1, "offset": 1},
        headers=_HEADERS,
    )
    assert [run["sweep_id"] for run in paged.json()["runs"]] == [oldest["sweep_id"]]
