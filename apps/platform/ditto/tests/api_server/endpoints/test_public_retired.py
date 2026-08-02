"""What a miner sees once a closed-generation submission is retired.

Peyton's requirement has two halves that pull against each other, so both are
pinned here: the row must leave the active queue (out of "Waiting for scores",
out of ``validator_queue_rank``, out of the waiting count) while staying
visible and searchable with its state shown.

Kept in its own module rather than added to ``test_public.py`` because the
queue-preview ordering in that file is being changed concurrently; nothing here
asserts on rank ordering, only on the retired row's absence from it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
    Score,
    SubmissionRetirement,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.tests.legacy_era import retired_era_writes_allowed

_T0 = datetime(2026, 7, 25, 12, tzinfo=UTC)
_ROLLOUT_START = _T0 - timedelta(days=3)
_CLOSED_VERSION = 6
_ACTIVE_VERSION = 7

# ``PublicActivityEntry.miner_hotkey`` is validated against the SS58 shape, so a
# placeholder like "5Miner-love-v8" is rejected before any assertion here runs.
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _hotkey(name: str) -> str:
    """A deterministic, SS58-shaped hotkey unique to ``name``."""
    digest = sha256(name.encode()).digest()
    body = "".join(_BASE58[byte % len(_BASE58)] for byte in digest)
    return ("5" + (body * 2))[:48]


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    bench_version: int,
    score_count: int,
    retired: bool = False,
    created_at: datetime | None = None,
) -> UUID:
    """Seed one submission, lifting the bench floor for the closed generation.

    Everything this module asserts is about a submission whose era has closed,
    so the v6 scores and tickets it needs are exactly the writes the
    bench-version floor now refuses. Production is not in that position: its v6
    rows were written before the floor and the ``NOT VALID`` constraints leave
    them alone. ``retired_era_writes_allowed`` reproduces that -- the retired
    rows land and are then grandfathered -- so what these tests read back is the
    ledger a miner with a closed generation actually has.
    """
    if bench_version < MIN_SCOREABLE_BENCH_VERSION:
        async with (
            maker() as floor_session,
            retired_era_writes_allowed(floor_session),
        ):
            return await _seed_rows(
                maker,
                name=name,
                bench_version=bench_version,
                score_count=score_count,
                retired=retired,
                created_at=created_at,
            )
    return await _seed_rows(
        maker,
        name=name,
        bench_version=bench_version,
        score_count=score_count,
        retired=retired,
        created_at=created_at,
    )


async def _seed_rows(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    bench_version: int,
    score_count: int,
    retired: bool = False,
    created_at: datetime | None = None,
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        # One activated v7 rollout for the whole module: it is what makes
        # ``active_bench_version`` return 7, and seeding a second would make the
        # active version ambiguous.
        if await session.scalar(select(BenchmarkRollout.rollout_id)) is None:
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
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_hotkey(name),
                name=name,
                version=1,
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=created_at or (_ROLLOUT_START - timedelta(days=2)),
            )
        )
        for index in range(score_count):
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=_hotkey(f"validator-{index}"),
                    status=TicketStatus.SCORED,
                    issued_at=_T0 - timedelta(hours=5),
                    deadline=_T0 - timedelta(hours=4),
                    bench_version=bench_version,
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=agent_id,
                    bench_version=bench_version,
                    validator_hotkey=_hotkey(f"validator-{index}"),
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
        if retired:
            session.add(
                SubmissionRetirement(
                    retirement_id=uuid4(),
                    agent_id=agent_id,
                    bench_version=bench_version,
                    superseded_by_version=_ACTIVE_VERSION,
                    actor="peyton",
                    reason="benchmark v6 is closed and will not be scored again",
                    expected_snapshot="ab" * 32,
                    score_count=score_count,
                    ticket_snapshot=[],
                    created_at=_T0,
                )
            )
    return agent_id


def _find(body: dict, agent_id: UUID) -> dict | None:
    """The activity entry for ``agent_id``, or None. For absence assertions."""
    return next((e for e in body["entries"] if e["agent_id"] == str(agent_id)), None)


def _entry(body: dict, agent_id: UUID) -> dict:
    """The activity entry for ``agent_id``, asserting it is present."""
    found = _find(body, agent_id)
    assert found is not None, f"{agent_id} missing from activity feed"
    return found


async def test_retired_row_leaves_the_waiting_queue_but_stays_visible(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    retired = await _seed(
        maker,
        name="love-v8",
        bench_version=_CLOSED_VERSION,
        score_count=0,
        retired=True,
    )
    waiting = await _seed(
        maker,
        name="ditto-v21-lean",
        bench_version=_ACTIVE_VERSION,
        score_count=1,
        created_at=_ROLLOUT_START + timedelta(hours=1),
    )
    _install(app, maker)

    response = await client.get("/api/v1/public/activity")
    assert response.status_code == 200, response.text
    body = response.json()

    retired_entry = _entry(body, retired)
    # Still there, with its state named.
    assert retired_entry["status"] == "retired"
    # Out of the queue: no rank to consume, and not counted as waiting.
    assert retired_entry["validator_queue_rank"] is None
    assert body["status_counts"].get("waiting_validator", 0) == 1
    assert body["status_counts"]["retired"] == 1
    # The current-generation submission keeps its place.
    assert _entry(body, waiting)["status"] == "waiting_validator"
    assert _entry(body, waiting)["validator_queue_rank"] == 1


async def test_retired_row_carries_no_retry_chip(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Restored retry slots on a closed generation must not read as progress."""
    retired = await _seed(
        maker,
        name="tfok-h01-v3",
        bench_version=_CLOSED_VERSION,
        score_count=2,
        retired=True,
    )
    _install(app, maker)

    body = (await client.get("/api/v1/public/activity")).json()
    entry = _entry(body, retired)
    assert entry["retry_state"] is None
    assert entry["retry_after"] is None


async def test_retired_row_is_still_findable_by_search(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    retired = await _seed(
        maker,
        name="xyx-winning",
        bench_version=_CLOSED_VERSION,
        score_count=1,
        retired=True,
    )
    _install(app, maker)

    body = (await client.get("/api/v1/public/activity?q=xyx-winning")).json()
    assert _entry(body, retired)["status"] == "retired"


async def test_retired_status_is_a_valid_activity_filter(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    retired = await _seed(
        maker,
        name="cedar",
        bench_version=_CLOSED_VERSION,
        score_count=0,
        retired=True,
    )
    await _seed(
        maker,
        name="current-work",
        bench_version=_ACTIVE_VERSION,
        score_count=1,
        created_at=_ROLLOUT_START + timedelta(hours=1),
    )
    _install(app, maker)

    response = await client.get("/api/v1/public/activity?status=retired")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [e["agent_id"] for e in body["entries"]] == [str(retired)]


async def test_retired_row_stays_reachable_by_direct_url(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    retired = await _seed(
        maker,
        name="dbk-v1",
        bench_version=_CLOSED_VERSION,
        score_count=2,
        retired=True,
    )
    _install(app, maker)

    response = await client.get(f"/api/v1/public/agent/{retired}/pipeline")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "retired"
    # Retiring a submission closes it out; it does not erase what it earned.
    # ``score_count`` is reported against the era the submission belongs to, so
    # the two v6 scores still read as 2 of 3 -- this page used to scope the
    # count to the ACTIVE benchmark and answer 0, which read to the miner as
    # though their accepted work had vanished along with their queue slot.
    # ``score_bench_version`` is what makes the number unambiguous: the count is
    # v6's, and the benchmark that is live now is a different one.
    assert body["score_count"] == 2
    assert body["score_bench_version"] == _CLOSED_VERSION
    assert body["active_bench_version"] == _ACTIVE_VERSION
    # Below its own era's quorum, so there is no finalized composite to show.
    assert body["final_composite"] is None


async def test_retired_row_is_dropped_from_the_operations_board(
    app: FastAPI, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    retired = await _seed(
        maker,
        name="zeus_v17",
        bench_version=_CLOSED_VERSION,
        score_count=2,
        retired=True,
    )
    _install(app, maker)

    response = await client.get("/api/v1/public/operations")
    assert response.status_code == 200, response.text
    activity = response.json()["activity"]
    assert activity["status_counts"].get("waiting_validator", 0) == 0
    assert _find(activity, retired) is None
