"""Admin read view of infrastructure-retry state (issue #475), on real Postgres."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import admin_screening_infra_retry as endpoint
from ditto.db.models import (
    Agent,
    AgentStatus,
    ScreeningAttempt,
    ScreeningRetryOverride,
    ValidatorQueueWithdrawal,
)
from ditto.db.queries import screening_infra_retry as infra
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.screening_infra_retry import infra_retry_delay
from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_URL = "/api/v1/admin/screening-infra-retries"
_CODE = infra.INFRA_AUTO_RETRY_REASON_CODES[0]
_MIN = timedelta(minutes=1)


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """A frozen endpoint clock, so every timestamp below is exact, not a margin."""
    frozen = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(endpoint, "_utcnow", lambda: frozen)
    return frozen


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _fast_id() -> UUID:
    """An attempt id whose jitter puts the first backoff near its 8 minute floor."""
    while True:
        candidate = uuid4()
        if infra_retry_delay(1, candidate) <= timedelta(minutes=8, seconds=5):
            return candidate


async def _failing_agent(
    maker: async_sessionmaker[AsyncSession],
    now: datetime,
    *,
    ago: timedelta,
    lane: str,
    failures: int = 1,
    fast: bool = False,
) -> UUID:
    """One parked agent; ``failures`` consecutive failures ending ``ago`` ago."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5HK-view-{agent_id.hex[:12]}",
                name=f"view-{agent_id.hex[:8]}",
                sha256=uuid4().hex * 2,
                status=AgentStatus.SCREENING_FAILED,
                created_at=now - timedelta(days=2),
                screening_policy_version=SCREENING_FLOOR_POLICY_VERSION,
            )
        )
        await session.flush()
        for index in range(failures):
            finished = now - ago - timedelta(hours=failures - 1 - index)
            last = index == failures - 1
            session.add(
                ScreeningAttempt(
                    attempt_id=_fast_id() if fast and last else uuid4(),
                    agent_id=agent_id,
                    screener_hotkey="5GScreenerHotkeyForInfraRetryViewTests000000000",
                    policy_version=SCREENING_FLOOR_POLICY_VERSION,
                    status="failed",
                    started_at=finished - 2 * _MIN,
                    deadline=finished + timedelta(minutes=60),
                    finished_at=finished,
                    reason_code=_CODE,
                    failure_provider="gcp",
                    failure_lane=lane,
                    public_reason="attempt",
                )
            )
    return agent_id


async def _get(client: httpx.AsyncClient) -> dict:
    response = await client.get(_URL, headers=_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


async def test_requires_the_admin_token(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    assert (await client.get(_URL)).status_code == 401
    wrong = {"Authorization": "Bearer not-the-token"}
    assert (await client.get(_URL, headers=wrong)).status_code == 401


async def test_empty_state_still_reports_policy(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    body = await _get(client)
    assert body["agents"] == []
    assert body["breakers"] == []
    assert body["agents_truncated"] is False
    assert body["summary"]["parked_agents"] == 0
    assert body["summary"]["by_state"] == {
        "backoff": 0,
        "breaker_held": 0,
        "probe_due": 0,
        "due": 0,
        "capped": 0,
    }
    assert "Derived from screening attempt history" in body["basis"]
    # A breaker holds workers on its provider only; the wording must say so.
    assert "worker on another provider can still claim" in body["basis"]
    assert "by backoff alone" in body["basis"]
    assert "no particular claimant" in body["basis"]


async def test_policy_block_equals_the_constants(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    policy = (await _get(client))["policy"]
    lookback = infra.BREAKER_HISTORY_LOOKBACK.total_seconds()
    assert policy == {
        "auto_retry_reason_codes": list(infra.INFRA_AUTO_RETRY_REASON_CODES),
        "base_backoff_seconds": infra.INFRA_RETRY_BASE_BACKOFF.total_seconds(),
        "max_backoff_seconds": infra.INFRA_RETRY_MAX_BACKOFF.total_seconds(),
        "jitter_fraction": infra.INFRA_RETRY_JITTER_FRACTION,
        "auto_retry_max_age_seconds": infra.INFRA_AUTO_RETRY_MAX_AGE.total_seconds(),
        "auto_retry_max_streak": infra.INFRA_AUTO_RETRY_MAX_STREAK,
        "plan_max_claimable": infra.INFRA_PLAN_MAX_CLAIMABLE,
        "breaker_distinct_agents": infra.BREAKER_DISTINCT_AGENTS,
        "breaker_window_seconds": infra.BREAKER_WINDOW.total_seconds(),
        "breaker_open_seconds": infra.BREAKER_OPEN_DURATION.total_seconds(),
        "breaker_probe_interval_seconds": infra.BREAKER_PROBE_INTERVAL.total_seconds(),
        "breaker_history_lookback_seconds": lookback,
    }


async def test_mixed_queue_reports_every_state_and_breaker(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    backoff = await _failing_agent(maker, now, ago=_MIN, lane="solo")
    due = await _failing_agent(maker, now, ago=15 * _MIN, lane="due")
    capped = await _failing_agent(
        maker,
        now,
        ago=_MIN,
        lane="capped",
        failures=infra.INFRA_AUTO_RETRY_MAX_STREAK,
    )
    withdrawn = await _failing_agent(maker, now, ago=_MIN, lane="withdrawn")
    # Three distinct agents inside the window open the breaker at -9 minutes;
    # their (fast) backoff has elapsed, its first probe is still a minute away.
    held = [
        await _failing_agent(
            maker, now, ago=(10 - i / 2) * _MIN, lane="held", fast=True
        )
        for i in range(3)
    ]
    # Opened 19 minutes ago, open window over: probes are due.
    probing = [
        await _failing_agent(maker, now, ago=(20 - i / 2) * _MIN, lane="probe")
        for i in range(3)
    ]
    async with maker() as session, session.begin():
        session.add(
            ValidatorQueueWithdrawal(
                withdrawal_id=uuid4(),
                agent_id=withdrawn,
                bench_version=await active_bench_version(session),
                actor="operator@example.com",
                reason="withdrawn from the era",
                expected_snapshot="x",
                score_count=0,
                ticket_snapshot=[],
                created_at=now - timedelta(days=1),
            )
        )
    _install(app, maker)

    body = await _get(client)

    rows = {UUID(row["agent_id"]): row for row in body["agents"]}
    assert len(rows) == 10
    assert rows[backoff]["state"] == "backoff"
    assert rows[backoff]["claim_outlook"] == "waiting_backoff"
    assert rows[backoff]["consecutive_failures"] == 1
    assert (rows[backoff]["provider"], rows[backoff]["lane"]) == ("gcp", "solo")
    assert rows[backoff]["reason_code"] == _CODE
    assert rows[backoff]["breaker_phase"] == "closed"
    assert rows[due]["state"] == "due"
    assert rows[due]["claim_outlook"] == "ready"
    assert rows[capped]["state"] == "capped"
    assert rows[capped]["claim_outlook"] == "needs_operator"
    assert rows[capped]["consecutive_failures"] == infra.INFRA_AUTO_RETRY_MAX_STREAK
    assert rows[withdrawn]["admitted"] is False
    assert rows[withdrawn]["claim_outlook"] == "not_admitted"
    for agent_id in held:
        assert rows[agent_id]["state"] == "breaker_held"
        assert rows[agent_id]["claim_outlook"] == "waiting_breaker"
        assert rows[agent_id]["breaker_phase"] == "open"
        assert rows[agent_id]["next_retry_at"] > rows[agent_id]["backoff_until"]
    for agent_id in probing:
        assert rows[agent_id]["breaker_phase"] == "half_open"
        assert rows[agent_id]["state"] == "probe_due"
        assert rows[agent_id]["claim_outlook"] == "ready"
    assert rows[due]["admitted"] is True

    assert body["summary"] == {
        "parked_agents": 10,
        "by_state": {
            "backoff": 2,
            "breaker_held": 3,
            "probe_due": 3,
            "due": 1,
            "capped": 1,
        },
        "not_admitted": 1,
        "aged_out_agents": 0,
        "open_breakers": 1,
        "half_open_breakers": 1,
        "breakers_total": body["summary"]["breakers_total"],
    }
    by_lane = {row["lane"]: row for row in body["breakers"]}
    assert by_lane["held"]["phase"] == "open"
    assert by_lane["held"]["parked_agents"] == 3
    assert by_lane["held"]["opened_at"] is not None
    assert by_lane["held"]["next_probe_at"] == by_lane["held"]["open_until"]
    assert by_lane["held"]["last_probe_at"] is None
    assert by_lane["probe"]["phase"] == "half_open"
    assert by_lane["solo"]["phase"] == "closed"
    assert by_lane["solo"]["opened_at"] is None
    assert by_lane["solo"]["parked_agents"] == 1
    # Unrecovered breakers (open, half-open) sort before closed ones.
    phases = [row["phase"] != "closed" for row in body["breakers"]]
    assert phases == sorted(phases, reverse=True)
    retry_times = [row["next_retry_at"] for row in body["agents"]]
    assert retry_times == sorted(retry_times)
    # No failure text, source, or miner identity leaks into the view.
    assert {"miner_hotkey", "public_reason", "error"}.isdisjoint(body["agents"][0])


async def test_output_is_bounded_and_totals_stay_visible(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    for lane in ("a", "b", "c", "d"):
        await _failing_agent(maker, now, ago=5 * _MIN, lane=lane)
    for i in range(3):
        await _failing_agent(maker, now, ago=(3 - i / 2) * _MIN, lane="open")
    _install(app, maker)
    monkeypatch.setattr(endpoint, "INFRA_RETRY_VIEW_MAX_AGENTS", 3)
    monkeypatch.setattr(endpoint, "INFRA_RETRY_VIEW_MAX_BREAKERS", 2)

    body = await _get(client)

    assert body["summary"]["parked_agents"] == 7
    assert len(body["agents"]) == 3
    assert body["agents_limit"] == 3
    assert body["agents_truncated"] is True
    assert body["summary"]["breakers_total"] == 5
    assert len(body["breakers"]) == 2
    assert body["breakers_truncated"] is True
    assert body["breakers"][0]["phase"] == "open"
    assert body["summary"]["open_breakers"] == 1
    assert body["summary"]["half_open_breakers"] == 0
    # Truncation keeps the longest-waiting (earliest next_retry_at) rows.
    retry_times = [row["next_retry_at"] for row in body["agents"]]
    assert retry_times == sorted(retry_times)


async def test_breaker_phase_follows_the_endpoint_clock(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    """Same breaker, three reads: open, then half-open, then no failure left."""
    for i in range(3):
        await _failing_agent(maker, now, ago=(4 - i / 2) * _MIN, lane="phase")
    _install(app, maker)

    body = await _get(client)
    (breaker,) = body["breakers"]
    assert breaker["phase"] == "open"
    assert datetime.fromisoformat(breaker["open_until"]) > now

    after_window = now + infra.BREAKER_OPEN_DURATION
    endpoint._utcnow = lambda: after_window  # restored by the ``now`` fixture
    body = await _get(client)
    (breaker,) = body["breakers"]
    assert breaker["phase"] == "half_open"
    assert body["summary"]["open_breakers"] == 0
    assert body["summary"]["half_open_breakers"] == 1
    assert {row["breaker_phase"] for row in body["agents"]} == {"half_open"}

    # A breaker that never opened is closed.
    await _failing_agent(maker, now, ago=_MIN, lane="lonely")
    lonely = [b for b in (await _get(client))["breakers"] if b["lane"] == "lonely"]
    assert lonely[0]["phase"] == "closed"


async def test_aged_out_agents_are_counted_but_not_listed(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    aged = await _failing_agent(
        maker, now, ago=infra.INFRA_AUTO_RETRY_MAX_AGE + _MIN, lane="aged"
    )
    # Two aged agents, so flipping the age bound (which would count only the fresh
    # capped one) changes the total instead of coinciding with it.
    aged_too = await _failing_agent(
        maker, now, ago=infra.INFRA_AUTO_RETRY_MAX_AGE + 2 * _MIN, lane="aged-2"
    )
    overridden = await _failing_agent(
        maker, now, ago=infra.INFRA_AUTO_RETRY_MAX_AGE + _MIN, lane="aged-override"
    )
    fresh_capped = await _failing_agent(
        maker,
        now,
        ago=_MIN,
        lane="capped",
        failures=infra.INFRA_AUTO_RETRY_MAX_STREAK,
    )
    async with maker() as session, session.begin():
        latest = await session.scalar(
            select(ScreeningAttempt.attempt_id).where(
                ScreeningAttempt.agent_id == overridden
            )
        )
        session.add(
            ScreeningRetryOverride(
                override_id=uuid4(),
                agent_id=overridden,
                attempt_id=latest,
                artifact_sha256="a" * 64,
                expected_score_count=0,
                force_full_review=False,
                actor="operator@example.com",
                reason="operator retry",
                created_at=now,
            )
        )
    _install(app, maker)

    body = await _get(client)

    assert body["summary"]["aged_out_agents"] == 2
    listed = {UUID(row["agent_id"]) for row in body["agents"]}
    assert listed == {fresh_capped}
    assert aged not in listed
    assert aged_too not in listed
    assert "aged_out_agents" in body["basis"]
