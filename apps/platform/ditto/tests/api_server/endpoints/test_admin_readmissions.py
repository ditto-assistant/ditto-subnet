"""Backroom visibility of SN-485 readmissions, fee accounting and cost basis."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server import readmission_cost_basis
from ditto.api_server.dependencies import get_session
from ditto.api_server.payment_verifier import VerifiedPayment
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    InferenceGrant,
    Score,
    UploadReadmission,
    ValidatorTicket,
)
from ditto.db.queries.payments import insert_readmission_payment

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_HOTKEY = "5ReadmitHotkey"
_COLDKEY = "5ReadmitColdkey"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    readmission_cost_basis._reset_cache()


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _agent(session: AsyncSession, name: str) -> UUID:
    agent_id = uuid4()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=_HOTKEY,
            name=name,
            version=1,
            sha256=agent_id.hex * 2,
            status=AgentStatus.SCORED,
            screening_policy_version=14,
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
    )
    await session.flush()
    return agent_id


def _verified(salt: int) -> VerifiedPayment:
    return VerifiedPayment(
        block_hash=f"0x{salt:064x}",
        extrinsic_index=3,
        miner_hotkey=_HOTKEY,
        miner_coldkey=_COLDKEY,
        amount_rao=75_000_000,
        tao_usd_rate=Decimal("400"),
        dest_address="5Dest",
        block_timestamp=datetime.now(UTC) - timedelta(hours=1),
    )


async def test_list_readmissions_reports_every_lane_and_the_fee_summary_adds_it(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    async with session_maker() as session, session.begin():
        admitted = await _agent(session, "admitted")
        refused = await _agent(session, "refused")
        lineage = await _agent(session, "lineage")
        rerun = await _agent(session, "rerun")
        session.add(
            EvaluationPayment(
                block_hash=f"0x{rerun.int:064x}",
                extrinsic_index=0,
                agent_id=rerun,
                miner_hotkey=_HOTKEY,
                miner_coldkey=_COLDKEY,
                amount_rao=75_000_000,
                dest_address="5Dest",
                timestamp=datetime.now(UTC),
            )
        )
        session.add(
            UploadReadmission(
                agent_id=rerun,
                kind="identical_rescore",
                lineage_agent_id=lineage,
                bench_version=19,
                miner_coldkey=_COLDKEY,
                lineage_evaluation_completed=True,
            )
        )
        await insert_readmission_payment(
            session,
            verified=_verified(1),
            agent_id=admitted,
            bench_version=19,
            rollout_id=None,
        )
        await insert_readmission_payment(
            session,
            verified=_verified(2),
            agent_id=refused,
            bench_version=19,
            rollout_id=None,
            outcome="credited",
            refusal_reason="a rollout opened after payment",
        )

    resp = await client.get("/api/v1/admin/readmissions", headers=_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["activation"]["active"] is False
    assert body["totals"]["admitted"] == 1
    assert body["totals"]["credited"] == 1
    assert body["totals"]["upload_readmissions"] == 1
    assert body["totals"]["upload_readmission_amount_rao"] == 75_000_000
    by_agent = {row["agent_id"]: row for row in body["paid"]}
    assert by_agent[str(admitted)]["outcome"] == "admitted"
    credited = by_agent[str(refused)]
    assert credited["outcome"] == "credited"
    assert credited["credit_spent"] is False
    assert credited["credit_expires_at"] is not None
    [upload] = body["uploads"]
    assert (upload["agent_id"], upload["kind"]) == (str(rerun), "identical_rescore")

    filtered = (
        await client.get(
            "/api/v1/admin/readmissions",
            params={"bench_version": 20},
            headers=_HEADERS,
        )
    ).json()
    assert filtered["paid"] == [] and filtered["uploads"] == []

    other = (
        await client.get(
            "/api/v1/admin/readmissions",
            params={"miner_coldkey": "5SomeoneElse"},
            headers=_HEADERS,
        )
    ).json()
    assert other["paid"] == [] and other["uploads"] == []
    # Totals follow the coldkey filter too.
    assert other["totals"]["admitted"] == 0
    assert other["totals"]["upload_readmissions"] == 0
    empty = await client.get(
        "/api/v1/admin/readmissions", params={"miner_coldkey": ""}, headers=_HEADERS
    )
    assert empty.status_code == 422

    fees = (await client.get("/api/v1/admin/miner-fees", headers=_HEADERS)).json()
    lane = fees["readmission"]
    assert lane["admitted"] == 1
    assert lane["admitted_amount_rao"] == 75_000_000
    assert lane["credited"] == 1
    assert lane["upload_readmissions"] == 1


async def _quorum_agent(session: AsyncSession, *, version: int, run_cost: int) -> None:
    agent_id = await _agent(session, f"costed-{uuid4().hex[:6]}")
    now = datetime.now(UTC)
    for index in range(3):
        validator = f"5Validator-{index}"
        deadline = now - timedelta(hours=1, minutes=index)
        session.add(
            Score(
                agent_id=agent_id,
                bench_version=version,
                validator_hotkey=validator,
                run_id=f"{agent_id}-{index}",
                signature=None,
                seed=index,
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=350,
                details={},
                generated_at=now,
            )
        )
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=version,
                validator_hotkey=validator,
                slot_id="slot-0",
                status=TicketStatus.SCORED,
                purpose=TicketPurpose.CANONICAL_QUORUM,
                purpose_revision=1,
                issued_at=deadline - timedelta(hours=1),
                deadline=deadline,
                attempt_count=1,
                manual_retry_grants=0,
                infra_retry_grants=0,
            )
        )
        await session.flush()
        session.add(
            InferenceGrant(
                grant_id=uuid4(),
                agent_id=agent_id,
                bench_version=version,
                validator_hotkey=validator,
                slot_id="slot-0",
                ticket_deadline=deadline,
                expires_at=deadline,
                status="exhausted",
                generation=1,
                allowed_models=["qwen/qwen3-32b"],
                request_budget=8192,
                request_count=100,
                token_budget=25_000_000,
                prompt_tokens=1000,
                completion_tokens=100,
                cost_microusd=run_cost,
                embedding_model="perplexity/pplx-embed-v1-0.6b",
                embedding_profile="dittobench-v8-pplx-embed-v1-0.6b-768-v1",
                embedding_provider="Perplexity",
                embedding_dimensions=768,
                embedding_request_budget=10_000,
                embedding_request_count=10,
                embedding_token_budget=5_000_000,
                embedding_tokens=1000,
                embedding_cost_microusd=0,
                usage_accounting_version=2,
                created_at=now - timedelta(hours=2),
                updated_at=now,
            )
        )
    await session.flush()


async def test_cost_basis_is_the_measured_median_or_null_without_enough_data(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    version = 15  # below the dueling floor: no season window applies
    async with session_maker() as session, session.begin():
        for cost in (100_000, 200_000, 300_000, 400_000):
            await _quorum_agent(session, version=version, run_cost=cost)
    async with session_maker() as session:
        thin = await readmission_cost_basis.readmission_cost_basis(
            session, bench_version=version
        )
    assert thin.agents_sampled == 4
    assert thin.median_agent_cost_usd is None

    readmission_cost_basis._reset_cache()
    async with session_maker() as session, session.begin():
        for cost in (500_000, 600_000):
            await _quorum_agent(session, version=version, run_cost=cost)
    async with session_maker() as session:
        basis = await readmission_cost_basis.readmission_cost_basis(
            session, bench_version=version
        )
    assert basis.agents_sampled == 6
    # Median of 0.10 .. 0.60 USD per run is 0.35; one evaluation is 3 runs.
    assert basis.median_run_cost_usd == pytest.approx(0.35)
    assert basis.median_agent_cost_usd == pytest.approx(1.05)


async def test_cost_basis_excludes_unranked_agents(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Agents the leaderboard does not rank never enter the published basis."""
    version = 15
    async with session_maker() as session, session.begin():
        for cost in (100_000, 200_000, 300_000, 400_000, 500_000):
            await _quorum_agent(session, version=version, run_cost=cost)
        # A sixth quorum agent that is shadow-only: graded, never ranked.
        await _quorum_agent(session, version=version, run_cost=9_000_000)
        shadow = (
            await session.scalars(
                select(Agent)
                .where(Agent.name.like("costed-%"))
                .order_by(Agent.created_at.desc())
            )
        ).first()
    async with session_maker() as session, session.begin():
        row = await session.get(Agent, shadow.agent_id)  # type: ignore[union-attr]
        assert row is not None
        row.shadow = True
    async with session_maker() as session:
        basis = await readmission_cost_basis.readmission_cost_basis(
            session, bench_version=version
        )
    assert basis.agents_sampled == 5
    assert basis.median_run_cost_usd == pytest.approx(0.3)


async def test_cost_basis_cache_keeps_only_the_newest_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired benchmark versions do not pin cache or lock entries forever."""
    measured: list[int] = []

    async def fake_measure(
        _session: object, *, bench_version: int
    ) -> readmission_cost_basis.CostBasis:
        measured.append(bench_version)
        return readmission_cost_basis.CostBasis(
            bench_version=bench_version,
            agents_sampled=0,
            runs_per_evaluation=3,
            median_run_cost_usd=None,
            median_agent_cost_usd=None,
            measured_at=datetime.now(UTC),
        )

    monkeypatch.setattr(readmission_cost_basis, "_measure", fake_measure)
    limit = readmission_cost_basis._MAX_CACHED_VERSIONS
    versions = list(range(10, 10 + limit + 3))
    for version in versions:
        await readmission_cost_basis.readmission_cost_basis(
            None,  # type: ignore[arg-type]
            bench_version=version,
        )
    newest = set(versions[-limit:])
    assert set(readmission_cost_basis._cache) == newest
    assert set(readmission_cost_basis._locks) == newest
    # A still-cached version is served without measuring again.
    await readmission_cost_basis.readmission_cost_basis(
        None,  # type: ignore[arg-type]
        bench_version=versions[-1],
    )
    assert measured == versions


async def test_cost_basis_eviction_keeps_a_lock_with_queued_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4607181307: a woken waiter and a new caller share one lock.

    While one task measures a retired version and another waits on its lock,
    newer versions force eviction. The lock must survive while it has users,
    so a fresh caller for the same version queues behind it and no two
    measurements of that version ever run at once.
    """
    import asyncio

    release = asyncio.Event()
    in_flight = 0
    peak = 0
    limit = readmission_cost_basis._MAX_CACHED_VERSIONS
    old = 5

    async def fake_measure(
        _session: object, *, bench_version: int
    ) -> readmission_cost_basis.CostBasis:
        nonlocal in_flight, peak
        if bench_version == old:
            in_flight += 1
            peak = max(peak, in_flight)
            await release.wait()
            in_flight -= 1
        return readmission_cost_basis.CostBasis(
            bench_version=bench_version,
            agents_sampled=0,
            runs_per_evaluation=3,
            median_run_cost_usd=None,
            median_agent_cost_usd=None,
            measured_at=datetime.now(UTC),
        )

    monkeypatch.setattr(readmission_cost_basis, "_measure", fake_measure)

    def call(version: int) -> asyncio.Task[readmission_cost_basis.CostBasis]:
        return asyncio.create_task(
            readmission_cost_basis.readmission_cost_basis(
                None,  # type: ignore[arg-type]
                bench_version=version,
            )
        )

    holder = call(old)
    await asyncio.sleep(0)
    waiter = call(old)
    await asyncio.sleep(0)
    held_lock = readmission_cost_basis._locks[old]
    # Newer versions evict; the retired version's lock has two users.
    for version in range(old + 1, old + 1 + limit + 1):
        await call(version)
    assert readmission_cost_basis._locks[old] is held_lock
    late = call(old)
    await asyncio.sleep(0)
    assert readmission_cost_basis._locks[old] is held_lock
    release.set()
    await asyncio.gather(holder, waiter, late)
    assert peak == 1
    assert old not in readmission_cost_basis._lock_users
    assert old not in readmission_cost_basis._locks
