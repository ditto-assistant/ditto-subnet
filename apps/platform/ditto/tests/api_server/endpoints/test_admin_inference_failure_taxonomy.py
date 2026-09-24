"""``/admin/inference-failure-taxonomy`` against a real ledger.

The 2026-09-22 incident is the fixture: a shared-pool ``upstream_http_429``
burst on one model where some failed rows carried the last attempted upstream
and some carried nothing at all. These tests pin the two claims an operator
acts on -- which model/route/code is burning, and how much of that is actually
*known* -- plus the boring case where nothing is wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    AgentStatus,
    InferenceGrant,
    InferenceRequest,
    ValidatorTicket,
)
from ditto.db.queries.inference_failure_taxonomy import (
    FAILURE_GROUP_LIMIT,
    load_inference_failure_taxonomy_rows,
)

pytestmark = pytest.mark.asyncio

_PATH = "/api/v1/admin/inference-failure-taxonomy"
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_BENCH_VERSION = 7
_BURST_MODEL = "openai/gpt-oss-20b"


def _install(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _grant(session: AsyncSession, now: datetime) -> InferenceGrant:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=f"miner-{uuid4().hex[:8]}",
        name="failure-taxonomy",
        sha256=uuid4().hex * 2,
        status=AgentStatus.EVALUATING,
        created_at=now,
    )
    ticket = ValidatorTicket(
        agent_id=agent.agent_id,
        validator_hotkey="validator-a",
        slot_id="slot-0",
        status=TicketStatus.ISSUED,
        issued_at=now - timedelta(hours=2),
        deadline=now + timedelta(minutes=20),
        bench_version=_BENCH_VERSION,
        attempt_count=1,
    )
    session.add_all([agent, ticket])
    await session.flush()
    grant = InferenceGrant(
        grant_id=uuid4(),
        agent_id=agent.agent_id,
        bench_version=_BENCH_VERSION,
        validator_hotkey=ticket.validator_hotkey,
        slot_id=ticket.slot_id,
        ticket_deadline=ticket.deadline,
        status="active",
        bearer_digest=None,
        broker_public_key=None,
        generation=1,
        allowed_models=[_BURST_MODEL],
        route_provider="Groq",
        route_profile="openrouter-route-test-v1",
        request_budget=10_000,
        token_budget=1_000_000,
        embedding_model="test-embedding",
        embedding_profile="openrouter-embedding-test-v1",
        embedding_provider="perplexity",
        embedding_dimensions=768,
        embedding_request_budget=1000,
        embedding_token_budget=1_000_000,
        embedding_request_count=0,
        embedding_tokens=0,
        embedding_cost_microusd=0,
        embedding_active_requests=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_microusd=0,
        active_requests=0,
        expires_at=ticket.deadline,
    )
    session.add(grant)
    await session.flush()
    return grant


def _rows(
    grant: InferenceGrant,
    *,
    count: int,
    started_at: datetime,
    status: str,
    kind: str = "chat",
    model: str = _BURST_MODEL,
    upstream_provider: str | None = None,
    terminal_error_code: str | None = None,
    openrouter_attempts: int = 1,
    fallback_phase: int = 0,
) -> list[InferenceRequest]:
    return [
        InferenceRequest(
            grant_id=grant.grant_id,
            nonce=uuid4(),
            generation=grant.generation,
            status=status,
            request_kind=kind,
            model=model,
            reserved_tokens=16,
            max_chargeable_tokens=4096,
            prompt_tokens=10 if status == "completed" else 0,
            completion_tokens=5 if status == "completed" else 0,
            cost_microusd=0,
            upstream_provider=upstream_provider,
            upstream_attempts=1,
            openrouter_attempts=openrouter_attempts,
            fallback_phase=fallback_phase,
            terminal_error_code=terminal_error_code,
            timed_out=False,
            latency_ms=None if status == "started" else 900,
            started_at=started_at,
            completed_at=None if status == "started" else started_at,
        )
        for _ in range(count)
    ]


def _lane(body: dict[str, Any], window: int, kind: str) -> dict[str, Any]:
    (lane,) = [
        row
        for row in body["lanes"]
        if row["window_seconds"] == window and row["request_kind"] == kind
    ]
    return lane


def _groups(body: dict[str, Any], window: int, kind: str) -> list[dict[str, Any]]:
    return [
        row
        for row in body["groups"]
        if row["window_seconds"] == window and row["request_kind"] == kind
    ]


def _one(groups: Iterable[dict[str, Any]], **match: Any) -> dict[str, Any]:
    (found,) = [
        row for row in groups if all(row[key] == value for key, value in match.items())
    ]
    return found


async def test_taxonomy_requires_the_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    assert (await client.get(_PATH)).status_code == 401
    wrong = await client.get(_PATH, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


async def test_a_burst_names_its_model_route_and_code_without_inventing_a_route(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The incident shape: 429s on one model, only some of them attributable."""
    _install(app, session_maker)
    now = datetime.now(UTC)
    recent = now - timedelta(seconds=20)
    async with session_maker() as session, session.begin():
        grant = await _grant(session, now)
        session.add_all(
            # The burst: the error envelope carried router metadata, so the
            # LAST ATTEMPTED upstream is known -- but the call did not succeed.
            _rows(
                grant,
                count=30,
                started_at=recent,
                status="failed",
                upstream_provider="Groq",
                terminal_error_code="upstream_http_429",
            )
            # Same burst, no router metadata on the error body at all.
            + _rows(
                grant,
                count=5,
                started_at=recent,
                status="failed",
                terminal_error_code="upstream_http_429",
            )
            # Successes on the same route, which is the only confirmed one.
            + _rows(
                grant,
                count=10,
                started_at=recent,
                status="completed",
                upstream_provider="Groq",
            )
            # A success that took in-request provider fallback (#2104).
            + _rows(
                grant,
                count=4,
                started_at=recent,
                status="completed",
                upstream_provider="Amazon Bedrock",
                openrouter_attempts=2,
            )
            # The embedding lane stamps its configured provider, never observes it.
            + _rows(
                grant,
                count=2,
                started_at=recent,
                status="completed",
                kind="embedding",
                model="test-embedding",
                upstream_provider="perplexity",
            )
            # In flight: no route and no code yet, and therefore not a group.
            + _rows(grant, count=1, started_at=recent, status="started")
        )
    response = await client.get(_PATH, headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "observed_at",
        "window_seconds",
        "group_limit",
        "lanes",
        "groups",
    }
    assert body["window_seconds"] == [60, 300, 900, 3600]

    chat = _lane(body, 60, "chat")
    assert chat["calls"] == 50
    assert chat["settled"] == 49
    assert chat["in_flight"] == 1
    assert chat["completed"] == 14
    assert chat["failed"] == 35
    assert chat["rate_limited_failures"] == 35
    assert chat["failure_share"] == pytest.approx(35 / 49, abs=1e-4)
    assert chat["groups_truncated"] is False

    chat_groups = _groups(body, 60, "chat")
    # Every settled row is grouped and the in-flight row is not.
    assert sum(row["calls"] for row in chat_groups) == 49

    attributed = _one(
        chat_groups, terminal_error_code="upstream_http_429", upstream_route="Groq"
    )
    assert attributed["failed"] == 30
    assert attributed["route_basis"] == "last_attempted"
    assert attributed["upstream_http_status"] == 429
    assert attributed["gateway"] == "openrouter"
    assert attributed["model"] == _BURST_MODEL
    assert attributed["share_of_settled_calls"] == pytest.approx(30 / 49, abs=1e-4)

    unknown = _one(
        chat_groups, terminal_error_code="upstream_http_429", upstream_route=None
    )
    assert unknown["failed"] == 5
    assert unknown["route_basis"] == "unknown"

    served = _one(chat_groups, terminal_error_code=None, upstream_route="Groq")
    assert served["completed"] == 10
    assert served["failed"] == 0
    assert served["route_basis"] == "confirmed_selected"
    assert served["upstream_http_status"] is None

    fellback = _one(chat_groups, upstream_route="Amazon Bedrock")
    assert fellback["completed"] == 4
    assert fellback["openrouter_attempts_max"] == 2

    # A failed row is never presented as a confirmed serving route.
    assert not [
        row
        for row in body["groups"]
        if row["route_basis"] == "confirmed_selected" and row["failed"] > 0
    ]

    embedding = _one(_groups(body, 60, "embedding"), upstream_route="perplexity")
    assert embedding["route_basis"] == "configured"
    assert embedding["gateway"] == "direct"
    assert _lane(body, 60, "embedding")["failed"] == 0


async def test_a_clean_window_is_reported_as_clean_next_to_an_older_burst(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Window bounds are real: the burst left the 1-minute read, not the 15."""
    _install(app, session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        grant = await _grant(session, now)
        session.add_all(
            _rows(
                grant,
                count=12,
                started_at=now - timedelta(seconds=20),
                status="completed",
                upstream_provider="Groq",
            )
            + _rows(
                grant,
                count=9,
                started_at=now - timedelta(seconds=400),
                status="failed",
                upstream_provider="Groq",
                terminal_error_code="upstream_http_429",
            )
        )
    body = (await client.get(_PATH, headers=_HEADERS)).json()

    clean = _lane(body, 60, "chat")
    assert (clean["calls"], clean["failed"], clean["rate_limited_failures"]) == (
        12,
        0,
        0,
    )
    assert clean["failure_share"] == 0.0
    assert [row["route_basis"] for row in _groups(body, 60, "chat")] == [
        "confirmed_selected"
    ]

    quiet_lane = _lane(body, 60, "embedding")
    assert (quiet_lane["calls"], quiet_lane["settled"]) == (0, 0)
    assert quiet_lane["failed"] == 0
    assert quiet_lane["failure_share"] == 0.0
    assert _groups(body, 60, "embedding") == []

    burst = _lane(body, 900, "chat")
    assert (burst["calls"], burst["failed"], burst["rate_limited_failures"]) == (
        21,
        9,
        9,
    )


async def test_unsafe_provider_text_never_reaches_the_operator_surface(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """``upstream_provider`` arrives inside an untrusted provider response."""
    _install(app, session_maker)
    smuggled = "Groq\nrate_limit_exceeded for key sk-live-0001"
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        grant = await _grant(session, now)
        session.add_all(
            _rows(
                grant,
                count=3,
                started_at=now - timedelta(seconds=20),
                status="failed",
                upstream_provider=smuggled,
                terminal_error_code="upstream_http_429",
            )
        )
    response = await client.get(_PATH, headers=_HEADERS)
    assert response.status_code == 200, response.text
    assert "sk-live-0001" not in response.text
    group = _one(_groups(response.json(), 60, "chat"), failed=3)
    assert group["upstream_route"] is None
    assert group["route_basis"] == "unrecognized"


async def test_group_list_is_capped_and_says_how_much_it_dropped(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Worst-first truncation: the burst survives the cap, the noise does not."""
    _install(app, session_maker)
    now = datetime.now(UTC)
    recent = now - timedelta(seconds=20)
    extra = FAILURE_GROUP_LIMIT + 1
    async with session_maker() as session, session.begin():
        grant = await _grant(session, now)
        rows: list[InferenceRequest] = _rows(
            grant,
            count=7,
            started_at=recent,
            status="failed",
            upstream_provider="Groq",
            terminal_error_code="upstream_http_429",
        )
        for index in range(extra):
            rows.extend(
                _rows(
                    grant,
                    count=1,
                    started_at=recent,
                    status="failed",
                    upstream_provider="Groq",
                    terminal_error_code=f"provider_unavailable_{index:03d}",
                )
            )
        session.add_all(rows)
    body = (await client.get(_PATH, headers=_HEADERS)).json()

    lane = _lane(body, 60, "chat")
    assert lane["failed"] == 7 + extra
    assert lane["groups_total"] == 1 + extra
    assert lane["groups_returned"] == FAILURE_GROUP_LIMIT
    assert lane["groups_truncated"] is True

    chat_groups = _groups(body, 60, "chat")
    assert len(chat_groups) == FAILURE_GROUP_LIMIT
    assert chat_groups[0]["terminal_error_code"] == "upstream_http_429"

    # The loader reports the same untruncated total under a tighter cap.
    async with session_maker() as session:
        _, groups = await load_inference_failure_taxonomy_rows(session, group_limit=1)
    minute = [
        row
        for row in groups
        if row["window_seconds"] == 60 and row["request_kind"] == "chat"
    ]
    assert len(minute) == 1
    assert minute[0]["groups_total"] == 1 + extra
    assert minute[0]["terminal_error_code"] == "upstream_http_429"
