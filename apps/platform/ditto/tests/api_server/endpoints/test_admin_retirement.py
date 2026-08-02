"""Audited close-out of submissions whose benchmark generation has ended.

The negative cases carry the weight here. Retirement is applied to somebody's
paid submission, so the tests that matter are the ones proving it *refuses*:
current-generation work, work already admitted to the new era by carryover, work
that already reached quorum, and work whose state moved after the operator
looked at it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutCarryover,
    Score,
    SubmissionRetirement,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.tests.legacy_era import retired_era_writes_allowed

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "peyton"}
_T0 = datetime(2026, 7, 25, 12, tzinfo=UTC)
_ROLLOUT_START = _T0 - timedelta(days=3)
_CLOSED_VERSION = 6
_ACTIVE_VERSION = 7
_CONFIRM = "RETIRE PREVIOUS GENERATION"
_REASON = "benchmark v6 is closed; v7 is the active generation"


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_active_era(maker: async_sessionmaker[AsyncSession]) -> None:
    """Make v7 the durable active benchmark, exactly as an activation would."""
    async with maker() as session, session.begin():
        session.add(
            BenchmarkRollout(
                rollout_id=uuid4(),
                from_version=_CLOSED_VERSION,
                desired_version=_ACTIVE_VERSION,
                status="activated",
                cohort_size=10,
                created_at=_ROLLOUT_START,
                activated_at=_ROLLOUT_START,
                rescore_cohort_target=10,
                priority_cohort_target=5,
            )
        )


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    bench_version: int,
    score_count: int,
    status: AgentStatus = AgentStatus.EVALUATING,
    created_at: datetime | None = None,
    live_ticket: bool = False,
) -> UUID:
    """Seed one submission's tickets and scores at ``bench_version``.

    The closed generation is the whole subject of this endpoint, so most calls
    here ask for v6 -- a version the score ledger and the ticket trigger now
    refuse outright. That is not a test smell to renumber away: retirement only
    ever runs against work left behind by an era that has ENDED, so a v7-only
    fixture would have nothing to retire. These rows are the ones production
    grandfathered when the floor landed, so they are written the same way, with
    the floor lifted only for the insert and restored immediately after.

    Current-generation calls (``bench_version=_ACTIVE_VERSION``) take the plain
    path and stay under the live floor, which is what proves the refusal cases
    are refused by the endpoint rather than by the database.
    """
    agent_id = uuid4()
    async with maker() as session, AsyncExitStack() as stack:
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            await stack.enter_async_context(retired_era_writes_allowed(session))
        async with session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"5Miner-{name}",
                    name=name,
                    version=1,
                    sha256=agent_id.hex * 2,
                    status=status,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=created_at or (_ROLLOUT_START - timedelta(days=2)),
                )
            )
            for index in range(3):
                scored = index < score_count
                if live_ticket and index == 2:
                    ticket_status = TicketStatus.ISSUED
                else:
                    ticket_status = (
                        TicketStatus.SCORED if scored else TicketStatus.EXPIRED
                    )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=f"validator-{index}",
                        status=ticket_status,
                        issued_at=_T0 - timedelta(hours=5 - index),
                        deadline=_T0 + timedelta(hours=5)
                        if ticket_status == TicketStatus.ISSUED
                        else _T0 - timedelta(hours=4 - index),
                        bench_version=bench_version,
                        attempt_count=1,
                        manual_retry_grants=0,
                    )
                )
                if scored:
                    session.add(
                        Score(
                            agent_id=agent_id,
                            bench_version=bench_version,
                            validator_hotkey=f"validator-{index}",
                            run_id=f"{name}-{index}",
                            seed=7,
                            composite=0.6,
                            tool_mean=0.6,
                            memory_mean=0.6,
                            median_ms=100,
                            n=114,
                            generated_at=_T0 - timedelta(hours=3),
                        )
                    )
    return agent_id


async def _preview(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/v1/admin/retirements", headers=_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _find(preview: dict, agent_id: UUID) -> dict | None:
    """The candidate row for ``agent_id``, or None. For absence assertions."""
    return next(
        (c for c in preview["candidates"] if c["agent_id"] == str(agent_id)), None
    )


def _candidate(preview: dict, agent_id: UUID) -> dict:
    """The candidate row for ``agent_id``, asserting it is present."""
    found = _find(preview, agent_id)
    assert found is not None, f"{agent_id} missing from preview"
    return found


async def test_preview_separates_the_three_previous_generation_populations(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """The conflation guard: "previous generation" is three groups, not one."""
    await _seed_active_era(maker)
    stranded = await _seed(
        maker, name="xyx-winning", bench_version=_CLOSED_VERSION, score_count=2
    )
    never = await _seed(
        maker, name="love-v8", bench_version=_CLOSED_VERSION, score_count=0
    )
    finalized = await _seed(
        maker,
        name="v6-champion",
        bench_version=_CLOSED_VERSION,
        score_count=3,
        status=AgentStatus.SCORED,
    )
    current = await _seed(
        maker,
        name="ditto-v21-lean",
        bench_version=_ACTIVE_VERSION,
        score_count=0,
        created_at=_ROLLOUT_START + timedelta(hours=1),
    )
    _install(app, maker)

    preview = await _preview(client)

    assert preview["active_bench_version"] == _ACTIVE_VERSION
    assert preview["eligible_count"] == 2
    assert preview["population_counts"] == {
        "partially_scored": 1,
        "never_scored": 1,
    }
    # The already-scored population is reported, and is NOT part of the
    # eligible set. This is the number a "they were already scored" instruction
    # actually describes.
    assert preview["finalized_prev_gen_count"] == 1
    assert _find(preview, finalized) is None
    assert _find(preview, current) is None
    assert _candidate(preview, stranded)["population"] == "partially_scored"
    assert _candidate(preview, never)["population"] == "never_scored"
    assert preview["bench_version_counts"] == {f"v{_CLOSED_VERSION}": 2}


async def test_retirement_records_actor_reason_and_the_superseding_version(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="dbk-v1", bench_version=_CLOSED_VERSION, score_count=2
    )
    _install(app, maker)
    snapshot = _candidate(await _preview(client), agent_id)["snapshot"]

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": snapshot,
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()["retirement"]
    assert body["bench_version"] == _CLOSED_VERSION
    assert body["superseded_by_version"] == _ACTIVE_VERSION
    assert body["actor"] == "peyton"
    assert body["reason"] == _REASON
    assert body["score_count"] == 2
    assert response.json()["idempotent"] is False

    # The agent row itself is untouched: no scoring semantics changed.
    async with maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.EVALUATING
        scores = list(
            await session.scalars(select(Score).where(Score.agent_id == agent_id))
        )
        assert len(scores) == 2


async def test_retirement_requires_the_confirmation_phrase(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="cedar", bench_version=_CLOSED_VERSION, score_count=0
    )
    _install(app, maker)
    snapshot = _candidate(await _preview(client), agent_id)["snapshot"]

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": snapshot,
            "reason": _REASON,
            "confirmation": "REMOVE FROM VALIDATOR QUEUE",
        },
    )
    assert response.status_code == 422, response.text


async def test_retirement_requires_an_operator_actor_header(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="cook-ditto", bench_version=_CLOSED_VERSION, score_count=2
    )
    _install(app, maker)
    snapshot = _candidate(await _preview(client), agent_id)["snapshot"]

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": snapshot,
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )
    assert response.status_code == 422, response.text


async def test_current_generation_work_can_never_be_retired(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker,
        name="happyDitto",
        bench_version=_ACTIVE_VERSION,
        score_count=2,
        created_at=_ROLLOUT_START + timedelta(hours=1),
    )
    _install(app, maker)

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": "0" * 64,
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )
    assert response.status_code == 409, response.text
    async with maker() as session:
        assert await session.scalar(select(SubmissionRetirement)) is None


async def test_a_carryover_adopted_submission_is_protected_from_retirement(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Carryover and retirement are opposite remedies; adoption wins first."""
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="adopted", bench_version=_CLOSED_VERSION, score_count=2
    )
    async with maker() as session, session.begin():
        rollout = await session.scalar(select(BenchmarkRollout))
        assert rollout is not None
        session.add(
            BenchmarkRolloutCarryover(
                rollout_id=rollout.rollout_id,
                agent_id=agent_id,
                position=1,
                frozen_score_count=2,
                frozen_owner_key="hotkey:5Miner-adopted",
                created_at=_T0,
            )
        )
    _install(app, maker)

    preview = await _preview(client)
    candidate = _candidate(preview, agent_id)
    assert candidate["retirement_allowed"] is False
    assert (
        candidate["blocking_reason"] == "submission is admitted to the active benchmark"
    )

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": candidate["snapshot"],
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )
    assert response.status_code == 409, response.text
    assert "admitted to the active benchmark" in response.json()["message"]


async def test_a_live_ticket_blocks_retirement(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker,
        name="still-running",
        bench_version=_CLOSED_VERSION,
        score_count=1,
        live_ticket=True,
    )
    _install(app, maker)

    response = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": "0" * 64,
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )
    assert response.status_code == 409, response.text


async def test_retirement_is_idempotent_by_request_id(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="gKat", bench_version=_CLOSED_VERSION, score_count=2
    )
    _install(app, maker)
    snapshot = _candidate(await _preview(client), agent_id)["snapshot"]
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "expected_snapshot": snapshot,
        "reason": _REASON,
        "confirmation": _CONFIRM,
    }

    first = await client.post(
        f"/api/v1/admin/retirements/{agent_id}", headers=_HEADERS, json=body
    )
    second = await client.post(
        f"/api/v1/admin/retirements/{agent_id}", headers=_HEADERS, json=body
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    async with maker() as session:
        rows = list(await session.scalars(select(SubmissionRetirement)))
        assert len(rows) == 1


async def test_batch_skips_a_moved_snapshot_and_applies_the_rest(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    good = await _seed(
        maker, name="zeus_v17", bench_version=_CLOSED_VERSION, score_count=2
    )
    stale = await _seed(
        maker, name="killer-6", bench_version=_CLOSED_VERSION, score_count=2
    )
    _install(app, maker)
    preview = await _preview(client)

    response = await client.post(
        "/api/v1/admin/retirements/batch",
        headers=_HEADERS,
        json={
            "reason": _REASON,
            "confirmation": _CONFIRM,
            "items": [
                {
                    "agent_id": str(good),
                    "request_id": str(uuid4()),
                    "expected_snapshot": _candidate(preview, good)["snapshot"],
                },
                {
                    "agent_id": str(stale),
                    "request_id": str(uuid4()),
                    "expected_snapshot": "0" * 64,
                },
            ],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retired"] == 1
    assert body["skipped"] == 1
    by_agent = {item["agent_id"]: item for item in body["results"]}
    assert by_agent[str(good)]["status"] == "retired"
    assert by_agent[str(stale)]["status"] == "skipped"
    assert by_agent[str(stale)]["detail"] == "validation state changed"
    async with maker() as session:
        rows = list(await session.scalars(select(SubmissionRetirement.agent_id)))
        assert rows == [good]


async def test_batch_rejects_duplicate_agents(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    _install(app, maker)
    agent_id = str(uuid4())
    response = await client.post(
        "/api/v1/admin/retirements/batch",
        headers=_HEADERS,
        json={
            "reason": _REASON,
            "confirmation": _CONFIRM,
            "items": [
                {
                    "agent_id": agent_id,
                    "request_id": str(uuid4()),
                    "expected_snapshot": "0" * 64,
                },
                {
                    "agent_id": agent_id,
                    "request_id": str(uuid4()),
                    "expected_snapshot": "0" * 64,
                },
            ],
        },
    )
    assert response.status_code == 422, response.text


async def test_a_retired_submission_stops_appearing_as_a_candidate(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_active_era(maker)
    agent_id = await _seed(
        maker, name="tfok-h01-v3", bench_version=_CLOSED_VERSION, score_count=2
    )
    _install(app, maker)
    snapshot = _candidate(await _preview(client), agent_id)["snapshot"]
    applied = await client.post(
        f"/api/v1/admin/retirements/{agent_id}",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": snapshot,
            "reason": _REASON,
            "confirmation": _CONFIRM,
        },
    )
    assert applied.status_code == 200, applied.text

    after = await _preview(client)
    assert _find(after, agent_id) is None
    assert after["eligible_count"] == 0
    assert after["already_retired_count"] == 1
