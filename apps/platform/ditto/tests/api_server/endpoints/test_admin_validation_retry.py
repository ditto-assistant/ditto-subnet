"""Regression coverage for audited validator-infrastructure recovery."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    Score,
    ScoreAuditEntry,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorQueueReinstatement,
    ValidatorQueueWithdrawal,
    ValidatorRetryRecovery,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_RELEASED,
    EVENT_SCORE_RETEST_REQUESTED,
    append_audit_entry,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.score_retests import activate_next_score_retest
from ditto.db.queries.tickets import (
    MAX_ATTEMPTS_PER_VERSION,
    issue_ticket,
    ticket_attempt_cap,
)
from ditto.tests.legacy_era import retired_era_writes_allowed
from ditto_screening_protocol.bench_v9 import (
    V9_SCORE_CONTRACT_MANIFEST_SHA256,
    V9_SCORE_CONTRACT_REVISION,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_T0 = datetime(2026, 7, 18, 12, tzinfo=UTC)
# Robust against the CI wall clock the endpoint reads via datetime.now(UTC):
# _PAST is always behind it, _FUTURE always ahead.
_PAST = _T0 - timedelta(hours=1)
_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
# The era the queue serves throughout this file. It used to be
# ``DEFAULT_BENCH_VERSION`` (2), which was never a statement about v2 -- it was
# "whatever era this fleet is on", and on an empty database that constant is
# also what ``active_bench_version`` answers, so the two lined up for free.
#
# They no longer can: v2 is retired, so no ticket may be issued and no score
# recorded under it. Every fixture here moves to the floor instead, and
# ``_activate_current_era`` below records the activation that makes the ledger
# agree -- the same pairing production has, just stated explicitly.
_BENCH_VERSION = MIN_SCOREABLE_BENCH_VERSION


@pytest.fixture
def retry_engine(engine: AsyncEngine) -> AsyncEngine:
    """Local alias for the root Postgres ``engine``."""
    return engine


@pytest.fixture
def retry_maker(
    retry_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(retry_engine, expire_on_commit=False)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _activate_current_era(session: AsyncSession) -> None:
    """Record the activation that puts the ledger's authority at ``_BENCH_VERSION``.

    With no rollout row at all, ``active_bench_version`` falls back to
    ``DEFAULT_BENCH_VERSION`` (2) -- an era nothing may be written under any
    more. Several answers here are computed against that authority: the
    reinstatement gate refuses a ticket whose era is no longer active, and the
    score-outlier scan reports only the era it scanned. Without this row those
    would compare live v7 fixtures against a v2 authority and refuse work that
    is in fact current.

    Idempotent, because a test seeds several submissions and only one rollout
    row may hold a given transition.
    """
    existing = await session.scalar(
        select(BenchmarkRollout.rollout_id).where(
            BenchmarkRollout.desired_version == _BENCH_VERSION
        )
    )
    if existing is not None:
        return
    session.add(
        BenchmarkRollout(
            rollout_id=uuid4(),
            from_version=_BENCH_VERSION - 1,
            desired_version=_BENCH_VERSION,
            status="activated",
            cohort_size=5,
            created_at=_T0 - timedelta(days=7),
            activated_at=_T0 - timedelta(days=6),
        )
    )


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    score_count: int = 0,
    bench_version: int = _BENCH_VERSION,
    composites: list[float] | None = None,
    ticket_count: int = 4,
    miner_hotkey: str = "5Miner",
) -> UUID:
    agent_id = uuid4()
    # Each call seeds an *independent* submission, so it needs its own identity:
    # ``agents_hotkey_name_version_key`` makes (miner_hotkey, name, version)
    # unique, and several tests here seed two or three agents. The name used to
    # be the constant "valid-agent", which only worked because that constraint
    # used to carry ``.ddl_if(dialect="postgresql")`` in ditto/db/models.py and
    # so was never created under SQLite's ``create_all``. No assertion in this
    # file reads the name this helper generates.
    #
    # A ``bench_version`` under the floor is only ever asked for by the two
    # tests whose subject IS a closed era, and those rows are the grandfathered
    # kind production still holds, so the floor is lifted just long enough to
    # write them and restored immediately.
    async with maker() as session, AsyncExitStack() as stack:
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            await stack.enter_async_context(retired_era_writes_allowed(session))
        async with session.begin():
            await _activate_current_era(session)
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=miner_hotkey,
                    name=f"valid-agent-{agent_id.hex[:8]}",
                    version=1,
                    sha256=agent_id.hex * 2,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_T0 - timedelta(days=1),
                    # A leasable submission in the current era is a screened one.
                    # The v2 contract this file used to run under required neither a
                    # verified image nor a versioned dataset, so a bare agent row was
                    # enough; every contract from v3 on requires both, and without
                    # them ``issue_ticket`` correctly answers "no job for you" and
                    # the eviction/reinstatement tests below lose their subject.
                    screened_image_sha256="a" * 64,
                    screened_image_size_bytes=1024,
                    screened_image_id="sha256:" + "b" * 64,
                    screened_image_ref=f"ditto-screen/{agent_id}:latest",
                    screened_image_upload_id=uuid4(),
                    screened_image_verified_at=_T0 - timedelta(hours=6),
                )
            )
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=bench_version,
                    seed=7,
                    sha256="d" * 64,
                    run_size="full",
                )
            )
            for index in range(ticket_count):
                hotkey = f"validator-{index}"
                status = (
                    TicketStatus.SCORED if index < score_count else TicketStatus.EXPIRED
                )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=hotkey,
                        status=status,
                        issued_at=_T0 - timedelta(hours=3 - index / 10),
                        deadline=_T0 - timedelta(hours=2 - index / 10),
                        bench_version=bench_version,
                        attempt_count=(
                            1
                            if status == TicketStatus.SCORED
                            else MAX_ATTEMPTS_PER_VERSION
                        ),
                        manual_retry_grants=0,
                        retry_after=_T0 - timedelta(hours=1),
                    )
                )
                if status == TicketStatus.SCORED:
                    session.add(
                        Score(
                            agent_id=agent_id,
                            bench_version=bench_version,
                            validator_hotkey=hotkey,
                            run_id=f"run-{index}",
                            seed=7,
                            composite=(composites or [0.7] * score_count)[index],
                            tool_mean=0.7,
                            memory_mean=0.7,
                            median_ms=100,
                            n=114,
                            generated_at=_T0 - timedelta(hours=1),
                        )
                    )
    return agent_id


async def _set_v9_score_contract(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    validator_hotkey: str = "validator-0",
    revision: str | None,
    manifest_sha256: str | None,
    rollout_mode: str | None,
    factor_bps: int = 10_000,
    model_use: dict[str, object] | None = None,
) -> None:
    async with maker() as session, session.begin():
        score = await session.get(Score, (agent_id, 9, validator_hotkey))
        assert score is not None
        score.details = {
            "v9_base": {
                "score_contract": {
                    "revision": revision,
                    "manifest_sha256": manifest_sha256,
                },
                "score_gates": {
                    "rollout_mode": rollout_mode,
                    **({"model_use": model_use} if model_use is not None else {}),
                },
                "semantic_gate_factor_bps": factor_bps,
            }
        }


async def _seed_online_heartbeat(
    maker: async_sessionmaker[AsyncSession], *, hotkey: str
) -> None:
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=hotkey,
                software_version="0.42.15",
                protocol_version=16,
                code_digest="d" * 64,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
            )
        )


async def test_retry_is_bound_to_the_ticket_and_score_epoch(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The recovery reports the ticket's era, not the ledger's active one.

    Stated with an era one ahead of the ledger authority, which is the shape a
    fleet has mid-rollout: the desired version is already being scored while
    ``from_version`` still holds authority. It used to be stated with v3, one
    era BEHIND -- the same property, read from the other side -- but no ticket
    or score may exist below the retired-era floor any more, so the only way to
    keep the two eras genuinely distinct is to look forward instead of back.
    """
    era = _BENCH_VERSION + 1
    agent_id = await _seed(retry_maker, score_count=1, bench_version=era)
    _install(app, retry_maker)

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert {ticket["bench_version"] for ticket in detail.json()["tickets"]} == {era}

    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": f"v{era} validator infrastructure recovery",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["recovery"]["bench_version"] == era


async def test_retry_grants_only_minimum_quorum_slots_and_preserves_history(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["recovery_allowed"] is True
    assert all(item["retry_budget_exhausted"] for item in detail.json()["tickets"])

    request_id = uuid4()
    payload = {
        "request_id": str(request_id),
        "expected_snapshot": detail.json()["snapshot"],
        "reason": "Sandbox OOM and writable-storage exhaustion verified by operator",
    }
    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json=payload,
        headers=_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["idempotent"] is False
    assert body["recovery"]["granted_validator_hotkeys"] == [
        "validator-0",
        "validator-1",
        "validator-2",
    ]
    retry = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json=payload,
        headers=_HEADERS,
    )
    assert retry.status_code == 200 and retry.json()["idempotent"] is True

    async with retry_maker() as session:
        agent = await session.get(Agent, agent_id)
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket)
                    .where(ValidatorTicket.agent_id == agent_id)
                    .order_by(ValidatorTicket.validator_hotkey)
                )
            ).all()
        )
        actions = list(
            (
                await session.scalars(
                    select(ValidatorRetryRecovery).where(
                        ValidatorRetryRecovery.agent_id == agent_id
                    )
                )
            ).all()
        )
    assert agent is not None and agent.status == AgentStatus.EVALUATING
    assert [ticket.attempt_count for ticket in tickets] == [
        MAX_ATTEMPTS_PER_VERSION
    ] * 4
    assert [ticket.manual_retry_grants for ticket in tickets] == [1, 1, 1, 0]
    assert len(actions) == 1
    assert len(actions[0].ticket_snapshot) == 4


async def test_one_score_grants_only_two_more_attempts_and_keeps_score(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, score_count=1)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": "Validator container loss corroborated by runtime exit evidence",
        },
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["recovery"]["granted_validator_hotkeys"] == [
        "validator-1",
        "validator-2",
    ]
    async with retry_maker() as session:
        score_total = await session.scalar(
            select(Score).where(Score.agent_id == agent_id)
        )
    assert score_total is not None


async def test_stale_snapshot_and_active_or_natural_retry_fail_closed(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    await _seed_online_heartbeat(retry_maker, hotkey="validator-0")
    _install(app, retry_maker)
    await client.get(f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS)
    stale = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": "0" * 64,
            "reason": "Verified validator infrastructure failure",
        },
        headers=_HEADERS,
    )
    assert stale.status_code == 409

    async with retry_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-0")
        )
        assert ticket is not None
        ticket.attempt_count = MAX_ATTEMPTS_PER_VERSION
        ticket.infra_retry_grants = 1
    changed = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert changed.json()["automatic_retry_available"] is False
    assert changed.json()["recovery_allowed"] is True
    granted = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": changed.json()["snapshot"],
            "reason": "Verified validator infrastructure failure",
        },
        headers=_HEADERS,
    )
    assert granted.status_code == 200


async def test_offline_natural_retry_does_not_block_healthy_sibling_recovery(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    async with retry_maker() as session, session.begin():
        offline = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-0")
        )
        assert offline is not None
        offline.attempt_count = 1
    for index in (1, 2, 3):
        await _seed_online_heartbeat(retry_maker, hotkey=f"validator-{index}")
    _install(app, retry_maker)

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["automatic_retry_available"] is False
    assert detail.json()["recovery_allowed"] is True

    granted = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": "Offline validator cannot consume its preserved retry slot",
        },
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["recovery"]["granted_validator_hotkeys"] == [
        "validator-1",
        "validator-2",
        "validator-3",
    ]


async def test_manual_grant_allows_exactly_one_more_same_version_issue(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": "Verified validator infrastructure failure",
        },
        headers=_HEADERS,
    )
    async with retry_maker() as session, session.begin():
        ticket = await issue_ticket(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC) + timedelta(seconds=1),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert ticket is not None and ticket.agent_id == agent_id
    assert ticket.attempt_count == MAX_ATTEMPTS_PER_VERSION + 1
    assert ticket.manual_retry_grants == 1


async def test_many_operator_grants_do_not_block_fresh_audited_recovery(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Audit history never becomes a lifetime ban on guarded recovery."""
    agent_id = await _seed(retry_maker, score_count=2)
    await _seed_online_heartbeat(retry_maker, hotkey="validator-2")
    _install(app, retry_maker)
    async with retry_maker() as session, session.begin():
        for index in range(20):
            session.add(
                ValidatorRetryRecovery(
                    recovery_id=uuid4(),
                    agent_id=agent_id,
                    actor="operator",
                    reason=f"prior verified infrastructure recovery {index}",
                    expected_snapshot=f"prior-snapshot-{index}",
                    score_count=2,
                    bench_version=_BENCH_VERSION,
                    ticket_snapshot=[],
                    granted_validator_hotkeys=["validator-2"],
                    created_at=_T0 - timedelta(days=20 - index),
                )
            )

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["recovery_allowed"] is True

    granted = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": "Fresh verified validator infrastructure recovery",
        },
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["recovery"]["granted_validator_hotkeys"] == ["validator-2"]

    after = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert after.status_code == 200, after.text
    assert len(after.json()["recoveries"]) == 21
    assert after.json()["automatic_retry_available"] is True
    assert after.json()["recovery_allowed"] is False
    assert after.json()["blocking_reason"] == (
        "operator-authorized validator retry is queued"
    )
    by_hotkey = {
        ticket["validator_hotkey"]: ticket for ticket in after.json()["tickets"]
    }
    assert by_hotkey["validator-2"]["retry_budget_exhausted"] is False
    assert by_hotkey["validator-3"]["retry_budget_exhausted"] is True

    listing = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    listed = next(
        item
        for item in listing.json()["submissions"]
        if item["agent_id"] == str(agent_id)
    )
    assert listed["retry_state"] == "retry_available"

    async with retry_maker() as session, session.begin():
        ticket = await issue_ticket(
            session,
            validator_hotkey="validator-2",
            now=datetime.now(UTC) + timedelta(seconds=1),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert ticket is not None and ticket.agent_id == agent_id
    assert ticket.attempt_count == MAX_ATTEMPTS_PER_VERSION + 1
    assert ticket.manual_retry_grants == 1


async def test_withdraw_failed_submission_preserves_history_and_stops_assignment(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["withdrawal_allowed"] is True

    request_id = uuid4()
    payload = {
        "request_id": str(request_id),
        "expected_snapshot": detail.json()["snapshot"],
        "reason": "Three validator scoring failures exhausted every retry budget",
        "confirmation": "REMOVE FROM VALIDATOR QUEUE",
    }
    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/withdraw",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["idempotent"] is False

    replay = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/withdraw",
        headers=_HEADERS,
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True

    triage = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert triage.status_code == 200
    assert all(row["agent_id"] != str(agent_id) for row in triage.json()["submissions"])

    async with retry_maker() as session, session.begin():
        issued = await issue_ticket(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC) + timedelta(seconds=1),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert issued is None

    async with retry_maker() as session:
        agent = await session.get(Agent, agent_id)
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
                )
            ).all()
        )
        withdrawal = await session.get(ValidatorQueueWithdrawal, request_id)
    assert agent is not None and agent.status == AgentStatus.EVALUATING
    assert len(tickets) == 4
    assert withdrawal is not None and len(withdrawal.ticket_snapshot) == 4


async def test_withdraw_fails_closed_when_snapshot_moves_or_ticket_is_active(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    stale = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/withdraw",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": "0" * 64,
            "reason": "Three validator scoring failures exhausted every retry budget",
            "confirmation": "REMOVE FROM VALIDATOR QUEUE",
        },
    )
    assert stale.status_code == 409

    async with retry_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-0")
        )
        assert ticket is not None
        ticket.status = TicketStatus.ISSUED
        ticket.deadline = _FUTURE
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.json()["withdrawal_allowed"] is False
    denied = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/withdraw",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": "Three validator scoring failures exhausted every retry budget",
            "confirmation": "REMOVE FROM VALIDATOR QUEUE",
        },
    )
    assert denied.status_code == 409


async def test_withdraw_remains_available_after_many_operator_recoveries(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    async with retry_maker() as session, session.begin():
        for index in range(4):
            session.add(
                ValidatorRetryRecovery(
                    recovery_id=uuid4(),
                    agent_id=agent_id,
                    actor="operator",
                    reason="Prior validator infrastructure recovery",
                    expected_snapshot=str(index) * 64,
                    score_count=0,
                    bench_version=_BENCH_VERSION,
                    ticket_snapshot=[],
                    granted_validator_hotkeys=[f"validator-{index}"],
                    created_at=_T0 + timedelta(minutes=index),
                )
            )
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["recovery_allowed"] is True
    assert detail.json()["withdrawal_allowed"] is True


_EVICT_CONFIRMATION = "EVICT LIVE VALIDATOR LEASES"
_EVICT_REASON = "mnemox-v55 hangs every lease and reports nothing; freeing the fleet"


async def _seed_live_lease(
    maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    validator_hotkey: str = "validator-0",
    slot_id: str = "slot-0",
    bench_version: int = _BENCH_VERSION,
    purpose: TicketPurpose = TicketPurpose.CANONICAL_QUORUM,
) -> datetime:
    """Seat the 2026-07-27 signature: a live 90-minute lease that never reported.

    No heartbeat, no progress, no ``fail_job`` — exactly the shape that the
    automatic liveness gate must (correctly) refuse to act on, and therefore
    exactly the shape an operator eviction has to be able to end anyway.
    """
    issued_at = datetime.now(UTC) - timedelta(minutes=20)
    deadline = issued_at + timedelta(minutes=90)
    async with maker() as session, AsyncExitStack() as stack:
        # A lease in a closed era can no longer be created -- that is the whole
        # point of the ticket trigger. One test below needs exactly that lease
        # to have existed before the floor landed, so it is written the same way
        # production's in-flight sub-v7 leases were: with the floor lifted, and
        # restored before anything under test runs.
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            await stack.enter_async_context(retired_era_writes_allowed(session))
        async with session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=validator_hotkey,
                    slot_id=slot_id,
                    status=TicketStatus.ISSUED,
                    purpose=purpose,
                    purpose_revision=1,
                    issued_at=issued_at,
                    deadline=deadline,
                    bench_version=bench_version,
                    attempt_count=1,
                )
            )
    return deadline


async def test_evict_frees_the_live_slot_and_preserves_every_source_record(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    burner = await _seed(retry_maker, ticket_count=0)
    waiting = await _seed(retry_maker, ticket_count=0, miner_hotkey="5OtherMiner")
    original_deadline = await _seed_live_lease(retry_maker, burner)
    _install(app, retry_maker)

    # Precondition: the slot is genuinely held. A claim from validator-0 resumes
    # the burner's lease rather than picking up the waiting submission.
    async with retry_maker() as session, session.begin():
        held = await issue_ticket(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert held is not None and held.agent_id == burner

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{burner}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["eviction_allowed"] is True
    assert body["live_ticket_count"] == 1

    request_id = uuid4()
    payload = {
        "request_id": str(request_id),
        "expected_snapshot": body["snapshot"],
        "reason": _EVICT_REASON,
        "confirmation": _EVICT_CONFIRMATION,
    }
    response = await client.post(
        f"/api/v1/admin/validation-retries/{burner}/evict",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 200, response.text
    evicted = response.json()
    assert evicted["idempotent"] is False
    assert evicted["freed_slots"] == 1
    assert evicted["eviction"]["evicted_validator_hotkeys"] == ["validator-0"]
    lease = evicted["evicted_leases"][0]
    assert lease["validator_hotkey"] == "validator-0"
    assert lease["slot_id"] == "slot-0"
    assert datetime.fromisoformat(lease["original_deadline"]) == original_deadline

    # The freed slot goes back to the pool now, not at the 90-minute deadline.
    async with retry_maker() as session, session.begin():
        reissued = await issue_ticket(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC) + timedelta(seconds=1),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert reissued is not None and reissued.agent_id == waiting

    # Eviction is not deletion: submission, artifact, screening verdict and the
    # complete ticket history all survive, and one audit row justifies the lease.
    async with retry_maker() as session:
        agent = await session.get(Agent, burner)
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket).where(ValidatorTicket.agent_id == burner)
                )
            ).all()
        )
        record = await session.get(ValidatorQueueWithdrawal, request_id)
        heartbeats = list((await session.scalars(select(ValidatorHeartbeat))).all())
        audits = list(
            (
                await session.scalars(
                    select(ValidatorLeaseAudit).where(
                        ValidatorLeaseAudit.agent_id == burner
                    )
                )
            ).all()
        )
    assert agent is not None
    assert agent.status == AgentStatus.EVALUATING
    assert agent.sha256 == burner.hex * 2
    assert agent.screening_policy_version == SCREENING_POLICY_VERSION
    assert len(tickets) == 1 and tickets[0].status == TicketStatus.EXPIRED
    # The revocation's ledger signature: the deadline was rewritten, not honoured.
    assert tickets[0].deadline < original_deadline
    # THE correctness property: no no-fault retry grant was minted. Granting one
    # would raise the cap on the artifact just evicted and rebuild the amplifier
    # that took mnemox-v55 to 9 attempts against a base budget of 2.
    assert tickets[0].infra_retry_grants == 0
    assert tickets[0].attempt_count == 1
    # And it got there with no heartbeat in the table at all — the automatic
    # liveness gate could not have admitted this, which is the entire point.
    assert heartbeats == []
    assert record is not None
    assert record.evicted_validator_hotkeys == ["validator-0"]
    assert len(record.ticket_snapshot) == 1
    assert len(audits) == 1
    audit = audits[0]
    assert audit.action == "operator_evicted"
    assert audit.reason == "operator_evicted_occupancy_unobservable"
    assert audit.context == "admin_queue_eviction"
    assert audit.evidence["operator_actor"] == "operator"
    assert audit.evidence["operator_reason"] == _EVICT_REASON
    assert audit.evidence["operator_request_id"] == str(request_id)
    assert audit.evidence["original_deadline"] == original_deadline.isoformat()

    # And it reaches the same terminal state a withdrawal does.
    triage = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert all(row["agent_id"] != str(burner) for row in triage.json()["submissions"])


async def _seed_capacity_heartbeat(
    maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    progress: dict[str, object] | None,
    validator_hotkey: str = "validator-0",
    slot_id: str = "slot-0",
) -> None:
    """A protocol-16 heartbeat that announces the slot as claimed.

    ``progress=None`` is v16's honest negative — leased, nothing to report — the
    report that makes "occupied but not progressing" an observation instead of an
    inference (ditto-subnet#274 / ditto-platform#499).
    """
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=validator_hotkey,
                software_version="0.35.0",
                protocol_version=16,
                code_digest="d" * 64,
                state="running_benchmark",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                benchmark_capacity={
                    "configured_slots": 1,
                    "healthy_slots": [slot_id],
                    "admission": "accepting",
                    "active": [
                        {
                            "slot_id": slot_id,
                            "agent_id": str(agent_id),
                            "bench_version": _BENCH_VERSION,
                            "progress": progress,
                            "healthy": True,
                        }
                    ],
                },
            )
        )


async def test_evict_never_mints_a_no_fault_retry_grant(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The single most important correctness property of the feature.

    ``force_expire_lease`` compensates the miner by default (#497): an automatic
    revocation raises ``infra_retry_grants`` so the attempt the coming reissue
    charges is not billed. An eviction must not do that. ditto-subnet#279
    established that the 2026-07-27 leases were *misclassified*, not silent —
    they carried ``fail_job(reason="infrastructure")``, the no-fault class — so
    every hang minted a grant, raised the cap and re-leased, which is how
    ``mnemox-v55`` reached nine attempts against a base budget of two with zero
    scores. Granting on eviction would rebuild that amplifier.

    Asserted as a contrast against the automatic path so the two cannot converge
    silently.
    """
    from ditto.db.queries.lease_liveness import LeaseLiveness, force_expire_lease

    automatic = await _seed(retry_maker, ticket_count=0)
    evicted = await _seed(retry_maker, ticket_count=0, miner_hotkey="5EvictedMiner")
    await _seed_live_lease(retry_maker, automatic)
    await _seed_live_lease(retry_maker, evicted, validator_hotkey="validator-1")

    # Automatic revocation on a proven-idle lease records evidence but does not
    # authorize another attempt.
    async with retry_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket, (automatic, _BENCH_VERSION, "validator-0")
        )
        assert ticket is not None
        await force_expire_lease(
            session,
            ticket=ticket,
            now=datetime.now(UTC),
            liveness=LeaseLiveness(
                idle=True, reason="idle_state_not_running_benchmark"
            ),
            context="issue_ticket",
        )
    async with retry_maker() as session:
        compensated = await session.get(
            ValidatorTicket, (automatic, _BENCH_VERSION, "validator-0")
        )
    assert compensated is not None and compensated.infra_retry_grants == 0

    # The same primitive, reached through eviction: the miner is NOT compensated.
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{evicted}", headers=_HEADERS
    )
    response = await client.post(
        f"/api/v1/admin/validation-retries/{evicted}/evict",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "reason": _EVICT_REASON,
            "confirmation": _EVICT_CONFIRMATION,
        },
    )
    assert response.status_code == 200, response.text
    async with retry_maker() as session:
        uncompensated = await session.get(
            ValidatorTicket, (evicted, _BENCH_VERSION, "validator-1")
        )
    assert uncompensated is not None
    assert uncompensated.infra_retry_grants == 0
    assert uncompensated.attempt_count == 1
    # Both caps are untouched: neither automated liveness expiry nor eviction
    # authorizes another attempt.
    assert ticket_attempt_cap(uncompensated) == MAX_ATTEMPTS_PER_VERSION
    assert ticket_attempt_cap(compensated) == MAX_ATTEMPTS_PER_VERSION


async def test_evict_records_what_the_platform_could_see_about_the_slot(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Protocol 16 turns "hung" from an inference into an observation.

    A v16 validator announces a claimed slot immediately and leaves ``progress``
    null until it has something to say, so the platform can now positively see
    *occupied and not progressing* — the shape a hang produces, and the shape the
    automatic gate still (correctly) refuses to act on for pre-v16 reporters.
    The eviction proceeds either way; what changes is what the audit row claims.
    """
    for index, (progress, expected) in enumerate(
        (
            (None, "operator_evicted_occupied_not_progressing"),
            (
                {
                    "stage": "running_benchmark",
                    "completed": 12,
                    "total": 114,
                    "ticket_deadline": _FUTURE.isoformat(),
                },
                "operator_evicted_occupied_progressing",
            ),
        )
    ):
        # Each iteration needs its own validator: a heartbeat row is keyed by
        # hotkey, and the two cases describe different fleet states.
        hotkey = f"validator-{index}"
        agent_id = await _seed(
            retry_maker, ticket_count=0, miner_hotkey=f"5Miner{index}"
        )
        await _seed_live_lease(retry_maker, agent_id, validator_hotkey=hotkey)
        await _seed_capacity_heartbeat(
            retry_maker, agent_id, progress=progress, validator_hotkey=hotkey
        )
        _install(app, retry_maker)

        detail = await client.get(
            f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
        )
        response = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/evict",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "expected_snapshot": detail.json()["snapshot"],
                "reason": _EVICT_REASON,
                "confirmation": _EVICT_CONFIRMATION,
            },
        )
        assert response.status_code == 200, response.text

        async with retry_maker() as session:
            audits = list(
                (
                    await session.scalars(
                        select(ValidatorLeaseAudit).where(
                            ValidatorLeaseAudit.agent_id == agent_id
                        )
                    )
                ).all()
            )
        assert len(audits) == 1
        assert audits[0].reason == expected
        assert audits[0].evidence["protocol_version"] == 16
        assert audits[0].evidence["reported_agent_id"] == str(agent_id)

        # The revocation is readable through the audit surface #498 shipped,
        # filtered by the action that distinguishes it from an inferred one.
        feed = await client.get(
            "/api/v1/admin/lease-revocations",
            headers=_HEADERS,
            params={"agent_id": str(agent_id), "action": "operator_evicted"},
        )
        assert feed.status_code == 200, feed.text
        assert feed.json()["total"] == 1
        assert feed.json()["revocations"][0]["reason"] == expected


async def test_evict_stops_a_submission_withdrawal_refuses_to_touch(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The gap this route exists for, stated as an assertion.

    ``/withdraw`` accepts only submissions that had already stopped consuming
    capacity, so on 2026-07-27 it refused every agent that was actively burning
    validator slots. Both of its refusals are reproduced here, and ``/evict``
    succeeds on both.
    """
    holding = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, holding)
    fresh = await _seed(retry_maker, ticket_count=0, miner_hotkey="5FreshMiner")
    _install(app, retry_maker)

    for agent_id, refusal in (
        (holding, "a validator ticket is still active"),
        (fresh, "submission can still reach quorum automatically"),
    ):
        detail = await client.get(
            f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
        )
        body = detail.json()
        assert body["withdrawal_allowed"] is False
        assert body["withdrawal_blocking_reason"] == refusal
        assert body["eviction_allowed"] is True
        assert body["eviction_blocking_reason"] is None

        refused = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/withdraw",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "expected_snapshot": body["snapshot"],
                "reason": _EVICT_REASON,
                "confirmation": "REMOVE FROM VALIDATOR QUEUE",
            },
        )
        assert refused.status_code == 409
        assert refusal in refused.text

        allowed = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/evict",
            headers=_HEADERS,
            json={
                "request_id": str(uuid4()),
                "expected_snapshot": body["snapshot"],
                "reason": _EVICT_REASON,
                "confirmation": _EVICT_CONFIRMATION,
            },
        )
        assert allowed.status_code == 200, allowed.text


async def test_evict_revokes_continual_retest_without_closing_scored_era(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, score_count=3, ticket_count=3)
    async with retry_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.SCORED
    await _seed_live_lease(
        retry_maker,
        agent_id,
        validator_hotkey="validator-retest",
        slot_id="slot-2",
        purpose=TicketPurpose.CONTINUAL_RETEST,
    )
    _install(app, retry_maker)

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["agent_status"] == AgentStatus.SCORED
    assert body["score_count"] == 3
    assert body["eviction_allowed"] is True
    assert body["eviction_blocking_reason"] is None
    assert body["live_ticket_count"] == 1
    retest = next(
        ticket
        for ticket in body["tickets"]
        if ticket["validator_hotkey"] == "validator-retest"
    )
    assert retest["purpose"] == TicketPurpose.CONTINUAL_RETEST
    assert retest["first_reported_at"] is None
    assert retest["status"] == "issued"

    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": body["snapshot"],
            "reason": _EVICT_REASON,
            "confirmation": _EVICT_CONFIRMATION,
        },
    )
    assert response.status_code == 200, response.text
    evicted = response.json()
    assert evicted["era_closed"] is False
    assert evicted["eviction"] is None
    assert evicted["freed_slots"] == 1
    assert evicted["evicted_leases"][0]["validator_hotkey"] == "validator-retest"
    assert evicted["evicted_leases"][0]["slot_id"] == "slot-2"

    async with retry_maker() as session:
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-retest")
        )
        withdrawal = await session.scalar(
            select(ValidatorQueueWithdrawal).where(
                ValidatorQueueWithdrawal.agent_id == agent_id
            )
        )
        agent = await session.get(Agent, agent_id)
    assert ticket is not None
    assert ticket.status == TicketStatus.EXPIRED
    assert withdrawal is None
    assert agent is not None and agent.status == AgentStatus.SCORED


async def test_evict_interlocks_reject_a_mismatched_or_malformed_call(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    snapshot = detail.json()["snapshot"]

    def _payload(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "request_id": str(uuid4()),
            "expected_snapshot": snapshot,
            "reason": _EVICT_REASON,
            "confirmation": _EVICT_CONFIRMATION,
        }
        base.update(overrides)
        return base

    stale = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json=_payload(expected_snapshot="0" * 64),
    )
    assert stale.status_code == 409
    assert "validation state changed" in stale.text

    # The removal route's phrase must never authorize an eviction: it is the one
    # confusion that would let an operator destroy live runs believing they were
    # tidying up dead ones.
    wrong_phrase = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json=_payload(confirmation="REMOVE FROM VALIDATOR QUEUE"),
    )
    assert wrong_phrase.status_code == 422

    for malformed in (
        _payload(expected_snapshot="not-a-snapshot"),
        _payload(reason="short"),
    ):
        response = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/evict",
            headers=_HEADERS,
            json=malformed,
        )
        assert response.status_code == 422, response.text

    no_actor = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers={"Authorization": _HEADERS["Authorization"]},
        json=_payload(),
    )
    assert no_actor.status_code == 422
    unauthorized = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict", json=_payload()
    )
    assert unauthorized.status_code == 401

    # Nothing above may have touched the lease.
    async with retry_maker() as session:
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-0")
        )
    assert ticket is not None and ticket.status == TicketStatus.ISSUED


async def test_evict_replay_is_idempotent_and_never_double_revokes(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    payload = {
        "request_id": str(uuid4()),
        "expected_snapshot": detail.json()["snapshot"],
        "reason": _EVICT_REASON,
        "confirmation": _EVICT_CONFIRMATION,
        "future_audit_field": "ignored",
    }
    first = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json=payload,
    )
    assert first.status_code == 200, first.text

    replay = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json=payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["freed_slots"] == 1
    assert replay.json()["eviction"]["evicted_validator_hotkeys"] == ["validator-0"]

    # A different request id against an already-removed era is a conflict, and a
    # replay must never write a second audit row for the same lease.
    conflict = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json={**payload, "request_id": str(uuid4())},
    )
    assert conflict.status_code == 409

    async with retry_maker() as session:
        audits = list(
            (
                await session.scalars(
                    select(ValidatorLeaseAudit).where(
                        ValidatorLeaseAudit.agent_id == agent_id
                    )
                )
            ).all()
        )
    assert len(audits) == 1


_REINSTATE_CONFIRMATION = "REINSTATE TO VALIDATOR QUEUE"
_REINSTATE_REASON = (
    "source review found no hang primitives; freeing the fleet was not a verdict"
)


async def _detail(client: httpx.AsyncClient, agent_id: UUID) -> dict:
    response = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _evict(client: httpx.AsyncClient, agent_id: UUID) -> httpx.Response:
    body = await _detail(client, agent_id)
    return await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/evict",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": body["snapshot"],
            "reason": _EVICT_REASON,
            "confirmation": _EVICT_CONFIRMATION,
        },
    )


async def _reinstate(
    client: httpx.AsyncClient, agent_id: UUID, **overrides: object
) -> httpx.Response:
    body = await _detail(client, agent_id)
    payload: dict[str, object] = {
        "request_id": str(uuid4()),
        "expected_snapshot": body["snapshot"],
        "reason": _REINSTATE_REASON,
        "confirmation": _REINSTATE_CONFIRMATION,
    }
    payload.update(overrides)
    return await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate",
        headers=_HEADERS,
        json=payload,
    )


async def test_reinstatement_returns_an_evicted_submission_to_the_queue(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point: eviction stops being one-way.

    Before this, an evicted miner's only route back was a fresh submission and a
    second evaluation fee, which made a capacity lever into a fine and is why the
    lever went unused. Reinstatement restores exactly the queue effect — the
    admission predicate, the triage feed, a real re-lease — and preserves the
    eviction record that justified taking it. With a one-attempt base budget,
    the separately audited retry remains required before the same validator can
    lease it again; fresh validators remain eligible for quorum.
    """
    from ditto.db.queries.benchmark_admission import agent_is_admitted

    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    _install(app, retry_maker)

    evicted = await _evict(client, agent_id)
    assert evicted.status_code == 200, evicted.text
    eviction_id = evicted.json()["eviction"]["eviction_id"]
    async with retry_maker() as session:
        assert not await agent_is_admitted(
            session, bench_version=_BENCH_VERSION, agent_id=agent_id
        )

    body = await _detail(client, agent_id)
    assert body["reinstatement_allowed"] is True
    assert body["reinstatement_blocking_reason"] is None
    assert body["withdrawal"]["reinstated_at"] is None
    assert body["reinstatement"] is None

    response = await _reinstate(client, agent_id, future_reinstatement_field="ignored")
    assert response.status_code == 200, response.text
    reinstated = response.json()
    assert reinstated["idempotent"] is False
    assert reinstated["restored_bench_version"] == _BENCH_VERSION
    assert reinstated["eviction"]["eviction_id"] == eviction_id
    assert reinstated["eviction"]["evicted_validator_hotkeys"] == ["validator-0"]
    assert reinstated["eviction"]["reinstated_at"] is not None
    assert reinstated["reinstatement"]["withdrawal_id"] == eviction_id
    assert reinstated["reinstatement"]["actor"] == "operator"
    assert reinstated["reinstatement"]["reason"] == _REINSTATE_REASON

    # Queue eligibility is back, but reinstatement is budget-neutral: the spent
    # one-attempt lease cannot print a retry by cycling eviction/reinstatement.
    async with retry_maker() as session:
        assert await agent_is_admitted(
            session, bench_version=_BENCH_VERSION, agent_id=agent_id
        )
    triage = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    row = next(
        row for row in triage.json()["submissions"] if row["agent_id"] == str(agent_id)
    )
    assert row["retry_state"] == "queued"
    async with retry_maker() as session, session.begin():
        reissued = await issue_ticket(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC) + timedelta(minutes=5),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert reissued is None

    # The same validator spent its base attempt, but another validator remains
    # free to contribute the next quorum input without any operator grant.
    async with retry_maker() as session, session.begin():
        reissued = await issue_ticket(
            session,
            validator_hotkey="validator-1",
            now=datetime.now(UTC) + timedelta(minutes=6),
            ttl=timedelta(minutes=90),
            bench_version=_BENCH_VERSION,
        )
    assert reissued is not None and reissued.agent_id == agent_id

    # The eviction record survives, resolved rather than deleted, and the lease
    # revocation it wrote is still readable through the #498 audit feed.
    async with retry_maker() as session:
        record = await session.get(ValidatorQueueWithdrawal, UUID(eviction_id))
        audits = list(
            (
                await session.scalars(
                    select(ValidatorLeaseAudit).where(
                        ValidatorLeaseAudit.agent_id == agent_id
                    )
                )
            ).all()
        )
    assert record is not None
    assert record.reinstated_at is not None
    assert record.evicted_validator_hotkeys == ["validator-0"]
    assert record.actor == "operator" and record.reason == _EVICT_REASON
    assert len(audits) == 1 and audits[0].action == "operator_evicted"
    feed = await client.get(
        "/api/v1/admin/lease-revocations",
        headers=_HEADERS,
        params={"agent_id": str(agent_id), "action": "operator_evicted"},
    )
    assert feed.status_code == 200, feed.text
    assert feed.json()["total"] == 1

    # And the operator surface still shows what happened, now resolved.
    body = await _detail(client, agent_id)
    assert body["withdrawal"]["reinstated_at"] is not None
    assert (
        body["reinstatement"]["reinstatement_id"]
        == (reinstated["reinstatement"]["reinstatement_id"])
    )
    assert body["reinstatement_allowed"] is False
    assert body["reinstatement_blocking_reason"] == (
        "removal has already been reinstated"
    )


async def test_reinstatement_interlocks_reject_a_wrong_phrase_or_stale_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Symmetric with the eviction route, including the phrase confusion.

    Three operator actions now write this table and each rejects the others'
    confirmation phrase, so no operator can reverse an eviction while believing
    they are taking one, or the other way round.
    """
    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    _install(app, retry_maker)
    assert (await _evict(client, agent_id)).status_code == 200

    stale = await _reinstate(client, agent_id, expected_snapshot="0" * 64)
    assert stale.status_code == 409
    assert "validation state changed" in stale.text

    for phrase in (_EVICT_CONFIRMATION, "REMOVE FROM VALIDATOR QUEUE"):
        wrong = await _reinstate(client, agent_id, confirmation=phrase)
        assert wrong.status_code == 422, wrong.text

    for malformed in (
        {"expected_snapshot": "not-a-snapshot"},
        {"reason": "short"},
    ):
        response = await _reinstate(client, agent_id, **malformed)
        assert response.status_code == 422, response.text

    body = await _detail(client, agent_id)
    payload = {
        "request_id": str(uuid4()),
        "expected_snapshot": body["snapshot"],
        "reason": _REINSTATE_REASON,
        "confirmation": _REINSTATE_CONFIRMATION,
    }
    no_actor = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate",
        headers={"Authorization": _HEADERS["Authorization"]},
        json=payload,
    )
    assert no_actor.status_code == 422
    unauthorized = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate", json=payload
    )
    assert unauthorized.status_code == 401

    # Nothing above may have reversed the eviction.
    async with retry_maker() as session:
        removals = list(
            (
                await session.scalars(
                    select(ValidatorQueueWithdrawal).where(
                        ValidatorQueueWithdrawal.agent_id == agent_id
                    )
                )
            ).all()
        )
        reinstatements = list(
            (
                await session.scalars(
                    select(ValidatorQueueReinstatement).where(
                        ValidatorQueueReinstatement.agent_id == agent_id
                    )
                )
            ).all()
        )
    assert len(removals) == 1 and removals[0].reinstated_at is None
    assert reinstatements == []


async def test_reinstatement_replays_idempotently_and_refuses_a_second_reversal(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    _install(app, retry_maker)
    assert (await _evict(client, agent_id)).status_code == 200

    body = await _detail(client, agent_id)
    payload = {
        "request_id": str(uuid4()),
        "expected_snapshot": body["snapshot"],
        "reason": _REINSTATE_REASON,
        "confirmation": _REINSTATE_CONFIRMATION,
    }
    first = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate",
        headers=_HEADERS,
        json=payload,
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate",
        headers=_HEADERS,
        json=payload,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["reinstatement"] == first.json()["reinstatement"]

    # A fresh request id against an already-reversed removal is a conflict, and
    # a reused id carrying different evidence is refused outright.
    again = await _reinstate(client, agent_id)
    assert again.status_code == 409
    assert "removal has already been reinstated" in again.text
    forged = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/reinstate",
        headers=_HEADERS,
        json={**payload, "reason": "a different reason entirely, same request id"},
    )
    assert forged.status_code == 409
    assert "request id already used" in forged.text

    async with retry_maker() as session:
        reinstatements = list(
            (
                await session.scalars(
                    select(ValidatorQueueReinstatement).where(
                        ValidatorQueueReinstatement.agent_id == agent_id
                    )
                )
            ).all()
        )
    assert len(reinstatements) == 1


async def test_reinstatement_allows_a_withdrawal_but_refuses_a_closed_era(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A withdrawal is reversible, but a closed-era removal remains inert.

    Reinstating a withdrawal restores eligibility without attempt budget; the
    ordinary retry route remains the separately audited way to make its
    exhausted ticket leaseable. No validator is ever issued a ticket for a
    closed era, so that case is still refused rather than accepted as a no-op.
    """
    withdrawn = await _seed(retry_maker)
    _install(app, retry_maker)
    body = await _detail(client, withdrawn)
    removed = await client.post(
        f"/api/v1/admin/validation-retries/{withdrawn}/withdraw",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": body["snapshot"],
            "reason": "Three validator scoring failures exhausted every retry budget",
            "confirmation": "REMOVE FROM VALIDATOR QUEUE",
        },
    )
    assert removed.status_code == 200, removed.text
    body = await _detail(client, withdrawn)
    assert body["reinstatement_allowed"] is True
    assert body["reinstatement_blocking_reason"] is None
    restored = await _reinstate(client, withdrawn)
    assert restored.status_code == 200, restored.text
    assert restored.json()["eviction"]["evicted_validator_hotkeys"] == []
    body = await _detail(client, withdrawn)
    assert body["withdrawal"]["reinstated_at"] is not None
    assert body["recovery_allowed"] is True
    retried = await client.post(
        f"/api/v1/admin/validation-retries/{withdrawn}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": body["snapshot"],
            "reason": "Verified validator infrastructure failure after withdrawal",
        },
    )
    assert retried.status_code == 200, retried.text

    # An eviction taken in a superseded era: the queue has moved on, so there is
    # nothing to come back to.
    old_era = _BENCH_VERSION - 1
    stale = await _seed(
        retry_maker, ticket_count=0, bench_version=old_era, miner_hotkey="5StaleMiner"
    )
    await _seed_live_lease(retry_maker, stale, bench_version=old_era)
    assert (await _evict(client, stale)).status_code == 200
    body = await _detail(client, stale)
    assert body["reinstatement_allowed"] is False
    assert body["reinstatement_blocking_reason"] == (
        f"benchmark era v{old_era} is no longer active "
        f"(the queue is now serving v{_BENCH_VERSION})"
    )
    closed = await _reinstate(client, stale)
    assert closed.status_code == 409
    assert "no longer active" in closed.text

    async with retry_maker() as session:
        record = await session.scalar(
            select(ValidatorQueueWithdrawal).where(
                ValidatorQueueWithdrawal.agent_id == stale
            )
        )
    assert record is not None and record.reinstated_at is None


async def test_reinstatement_cannot_launder_the_retry_budget(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The security property of the change, stated as an assertion.

    Eviction refuses to compensate the miner (``compensate=False``) so it cannot
    raise the attempt cap on the artifact it just evicted. If reinstatement gave
    that cap back, the pair would be an attempt printer: evict, reinstate,
    collect, repeat — free leases past ``MAX_AGENT_INFRA_RETRY_GRANTS`` (12 per
    agent per era, #522), rebuilding the amplifier that took ``mnemox-v55`` to
    nine attempts against a base budget of two. So the cycle is run three times
    and every counter is asserted unchanged across all of it.
    """
    agent_id = await _seed(retry_maker, ticket_count=0)
    await _seed_live_lease(retry_maker, agent_id)
    async with retry_maker() as session, session.begin():
        session.add(
            ValidatorRetryRecovery(
                recovery_id=uuid4(),
                agent_id=agent_id,
                actor="operator",
                reason="Prior validator infrastructure recovery",
                expected_snapshot="a" * 64,
                score_count=0,
                bench_version=_BENCH_VERSION,
                ticket_snapshot=[],
                granted_validator_hotkeys=["validator-0"],
                created_at=_T0,
            )
        )
    _install(app, retry_maker)

    async def _budget() -> tuple[int, int, int, int]:
        async with retry_maker() as session:
            tickets = list(
                (
                    await session.scalars(
                        select(ValidatorTicket).where(
                            ValidatorTicket.agent_id == agent_id
                        )
                    )
                ).all()
            )
            recoveries = len(
                list(
                    (
                        await session.scalars(
                            select(ValidatorRetryRecovery).where(
                                ValidatorRetryRecovery.agent_id == agent_id
                            )
                        )
                    ).all()
                )
            )
        return (
            max(ticket.attempt_count for ticket in tickets),
            sum(ticket.infra_retry_grants for ticket in tickets),
            sum(ticket.manual_retry_grants for ticket in tickets),
            recoveries,
        )

    before = await _budget()
    assert before == (1, 0, 0, 1)

    for _cycle in range(3):
        assert (await _evict(client, agent_id)).status_code == 200
        response = await _reinstate(client, agent_id)
        assert response.status_code == 200, response.text
        recorded = response.json()["reinstatement"]["retry_budget_snapshot"]
        # The route records the counts it left alone, so "nothing was granted"
        # is checkable from the audit row and not only from this test.
        assert recorded["attempts_used"] == before[0]
        assert recorded["agent_infra_retry_grants"] == before[1]
        assert recorded["max_agent_infra_retry_grants"] == 0
        assert recorded["manual_retry_grants"] == before[2]
        assert recorded["operator_recoveries"] == before[3]
        assert recorded["max_operator_recoveries"] is None
        assert await _budget() == before
        # Each cycle needs its own live lease to evict; the reinstated one is
        # expired, and re-leasing it is the validator's job, not the operator's.
        await _seed_live_lease(
            retry_maker, agent_id, validator_hotkey=f"validator-cycle-{_cycle}"
        )

    # Three evict/reinstate cycles bought exactly nothing: no no-fault grant, no
    # manual grant, and the operator-recovery count is still spent.
    assert await _budget() == before
    async with retry_maker() as session:
        removals = list(
            (
                await session.scalars(
                    select(ValidatorQueueWithdrawal).where(
                        ValidatorQueueWithdrawal.agent_id == agent_id
                    )
                )
            ).all()
        )
    # Every eviction is preserved; each is resolved by its own reversal.
    assert len(removals) == 3
    assert all(row.reinstated_at is not None for row in removals)


async def test_triage_tells_a_silent_expiry_apart_from_a_reported_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Not being able to tell these apart is what let the incident run unseen."""
    agent_id = await _seed(retry_maker, ticket_count=3)
    async with retry_maker() as session, session.begin():
        reported = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-0")
        )
        assert reported is not None
        reported.failure_reason = "scoring_error"
        reported.failed_at = reported.issued_at + timedelta(minutes=5)
        superseded = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-1")
        )
        assert superseded is not None
        # A failure kept from a lease that has since been re-leased is history,
        # not a report about this attempt.
        superseded.failure_reason = "scoring_error"
        superseded.failed_at = superseded.issued_at - timedelta(hours=1)
    _install(app, retry_maker)

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    silent = {
        ticket["validator_hotkey"]: ticket["silently_expired"]
        for ticket in detail.json()["tickets"]
    }
    assert silent == {
        "validator-0": False,
        "validator-1": True,
        "validator-2": True,
    }

    triage = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    rows = [
        item
        for item in triage.json()["submissions"]
        if item["agent_id"] == str(agent_id)
    ]
    assert len(rows) == 1
    assert rows[0]["silent_expiry_count"] == 2


async def test_replace_one_validators_score_and_reissue_same_ticket(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, score_count=3)
    _install(app, retry_maker)
    async with retry_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.SCORED
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-1")
        )
        assert ticket is not None
        ticket.purpose = TicketPurpose.CONTINUAL_RETEST

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-1",
        headers=_HEADERS,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["replacement_allowed"] is True
    request_id = uuid4()
    payload = {
        "request_id": str(request_id),
        "expected_snapshot": detail.json()["snapshot"],
        "expected_run_id": "run-1",
        "reason": "Validator relay failure made this accepted score untrustworthy",
    }
    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-1/replace-score",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["preserved_score_count"] == 3
    assert response.json()["original_run_id"] == "run-1"

    replay = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-1/replace-score",
        headers=_HEADERS,
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True

    async with retry_maker() as session:
        agent = await session.get(Agent, agent_id)
        scores = list(
            (
                await session.scalars(select(Score).where(Score.agent_id == agent_id))
            ).all()
        )
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-1")
        )
        audit = await session.scalar(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
            )
        )
    assert agent is not None and agent.status == AgentStatus.SCORED
    assert {score.validator_hotkey for score in scores} == {
        "validator-0",
        "validator-1",
        "validator-2",
    }
    assert ticket is not None and ticket.status == TicketStatus.ISSUED
    assert ticket.purpose == TicketPurpose.CANONICAL_QUORUM
    assert ticket.attempt_count == 2
    assert audit is not None
    assert audit.payload["actor"] == "operator"
    assert audit.payload["reason"] == payload["reason"]
    assert audit.payload["run_id"] == "run-1"
    assert audit.payload["preserved_score"]["composite"] == 0.7
    assert audit.payload["preserved_score"]["bench_version"] == (_BENCH_VERSION)

    pending = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-1",
        headers=_HEADERS,
    )
    assert pending.json()["replacement_pending"] is True
    assert pending.json()["replacement_allowed"] is False

    release = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-1/release-ticket",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": pending.json()["snapshot"],
            "expected_deadline": pending.json()["ticket_deadline"],
            "reason": "Operator released the re-test after validator evidence cleared",
        },
    )
    assert release.status_code == 200, release.text
    assert release.json()["status"] == "scored"
    async with retry_maker() as session:
        ticket = await session.get(
            ValidatorTicket, (agent_id, _BENCH_VERSION, "validator-1")
        )
        released = await session.scalar(
            select(ScoreAuditEntry).where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_RELEASED,
            )
        )
    assert ticket is not None and ticket.status == TicketStatus.SCORED
    assert released is not None


async def test_replace_score_fails_closed_on_run_change_or_busy_validator(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, score_count=1)
    other_agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-0",
        headers=_HEADERS,
    )
    wrong_run = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-0/replace-score",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": detail.json()["snapshot"],
            "expected_run_id": "different-run",
            "reason": "Verified validator infrastructure failure changed the result",
        },
    )
    assert wrong_run.status_code == 409

    async with retry_maker() as session, session.begin():
        other = await session.get(
            ValidatorTicket,
            (other_agent_id, _BENCH_VERSION, "validator-0"),
        )
        assert other is not None
        other.status = TicketStatus.ISSUED
        other.deadline = datetime.now(UTC) + timedelta(minutes=30)
    blocked = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}/validators/validator-0",
        headers=_HEADERS,
    )
    assert blocked.status_code == 200
    assert blocked.json()["replacement_allowed"] is False
    assert blocked.json()["blocking_reason"] == (
        "validator is currently assigned to another submission"
    )


async def test_v9_contract_retest_preview_is_exact_and_includes_evaluating_agents(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    shadow = await _seed(retry_maker, score_count=2, bench_version=9, ticket_count=2)
    missing = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    current = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    await _set_v9_score_contract(
        retry_maker,
        agent_id=shadow,
        revision="v9-base-shadow-calibration-v1",
        manifest_sha256="5" * 64,
        rollout_mode="shadow",
        factor_bps=0,
    )
    await _set_v9_score_contract(
        retry_maker,
        agent_id=shadow,
        validator_hotkey="validator-1",
        revision=V9_SCORE_CONTRACT_REVISION,
        manifest_sha256=V9_SCORE_CONTRACT_MANIFEST_SHA256,
        rollout_mode="enforce",
    )
    # A score with no v9_base at all is also a mismatch; it cannot be treated
    # as current merely because its ordinary composite is positive.
    await _set_v9_score_contract(
        retry_maker,
        agent_id=current,
        revision=V9_SCORE_CONTRACT_REVISION,
        manifest_sha256=V9_SCORE_CONTRACT_MANIFEST_SHA256,
        rollout_mode="enforce",
        factor_bps=0,
    )
    async with retry_maker() as session, session.begin():
        missing_agent = await session.get(Agent, missing)
        assert missing_agent is not None
        missing_agent.status = AgentStatus.SCORED
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["required_revision"] == V9_SCORE_CONTRACT_REVISION
    assert body["required_manifest_sha256"] == V9_SCORE_CONTRACT_MANIFEST_SHA256
    assert body["required_rollout_mode"] == "enforce"
    assert body["count"] == 2
    by_agent = {item["agent_id"]: item for item in body["items"]}
    assert set(by_agent) == {str(shadow), str(missing)}
    assert by_agent[str(shadow)]["agent_status"] == "evaluating"
    assert by_agent[str(shadow)]["observed_rollout_mode"] == "shadow"
    assert by_agent[str(shadow)]["semantic_gate_factor_bps"] == 0
    assert by_agent[str(shadow)]["queue_allowed"] is True
    assert by_agent[str(missing)]["observed_revision"] is None
    assert str(current) not in by_agent


async def test_v9_contract_retest_includes_only_proven_case_attribution_defects(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    defective = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    genuine_zero = await _seed(
        retry_maker, score_count=1, bench_version=9, ticket_count=1
    )
    healthy = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    common = {
        "revision": V9_SCORE_CONTRACT_REVISION,
        "manifest_sha256": V9_SCORE_CONTRACT_MANIFEST_SHA256,
        "rollout_mode": "enforce",
    }
    await _set_v9_score_contract(
        retry_maker,
        agent_id=defective,
        factor_bps=0,
        model_use={
            "result": "insufficient_evidence",
            "case_attribution_complete": False,
            "successful_requests": 734,
        },
        **common,
    )
    await _set_v9_score_contract(
        retry_maker,
        agent_id=genuine_zero,
        factor_bps=0,
        model_use={
            "result": "zero_inference",
            "case_attribution_complete": False,
            "successful_requests": 0,
        },
        **common,
    )
    await _set_v9_score_contract(
        retry_maker,
        agent_id=healthy,
        factor_bps=10_000,
        model_use={
            "result": "passed",
            "case_attribution_complete": True,
            "successful_requests": 734,
        },
        **common,
    )
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["agent_id"] for item in items] == [str(defective)]
    assert items[0]["observed_rollout_mode"] == "enforce"
    assert items[0]["semantic_gate_factor_bps"] == 0
    assert items[0]["queue_allowed"] is True


async def test_v9_contract_retest_requires_typed_confirmation_and_current_snapshot(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    await _set_v9_score_contract(
        retry_maker,
        agent_id=agent_id,
        revision="v9-base-shadow-calibration-v1",
        manifest_sha256="5" * 64,
        rollout_mode="shadow",
    )
    _install(app, retry_maker)
    preview = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    item = preview.json()["items"][0]
    payload = {
        "basis": "v9_contract_mismatch",
        "reason": "Replace a signed shadow score with the authoritative v9 contract",
        "items": [
            {
                "agent_id": str(agent_id),
                "request_id": str(uuid4()),
                "expected_snapshot": item["snapshot"],
                "expected_run_id": item["run_id"],
            }
        ],
    }
    missing_confirmation = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json=payload,
    )
    assert missing_confirmation.status_code == 422
    wrong_confirmation = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={**payload, "confirmation": "QUEUE RETESTS"},
    )
    assert wrong_confirmation.status_code == 422

    async with retry_maker() as session, session.begin():
        score = await session.get(Score, (agent_id, 9, "validator-0"))
        assert score is not None
        score.details = {
            "v9_base": {
                "score_contract": {
                    "revision": V9_SCORE_CONTRACT_REVISION,
                    "manifest_sha256": V9_SCORE_CONTRACT_MANIFEST_SHA256,
                },
                "score_gates": {"rollout_mode": "enforce"},
                "semantic_gate_factor_bps": 10_000,
            }
        }
    stale = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={**payload, "confirmation": "QUEUE V9 CONTRACT RETESTS"},
    )
    assert stale.status_code == 200, stale.text
    assert stale.json()["skipped"] == 1
    assert "state changed" in stale.json()["results"][0]["detail"]


async def test_v9_contract_retest_waits_for_reused_continual_ticket_then_reclaims_it(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A mutable continual ticket must not erase accepted-score repair intent."""
    agent_id = await _seed(
        retry_maker,
        score_count=1,
        bench_version=9,
        ticket_count=1,
    )
    await _set_v9_score_contract(
        retry_maker,
        agent_id=agent_id,
        revision="v9-base-shadow-calibration-v1",
        manifest_sha256="5" * 64,
        rollout_mode="shadow",
    )
    async with retry_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket,
            (agent_id, 9, "validator-0"),
        )
        assert ticket is not None
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CONTINUAL_RETEST
        ticket.purpose_revision += 1
        ticket.issued_at = datetime.now(UTC)
        ticket.deadline = datetime.now(UTC) + timedelta(minutes=90)
    _install(app, retry_maker)

    preview = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    [item] = preview.json()["items"]
    assert item["ticket_status"] == "issued"
    assert item["queue_allowed"] is True

    queued = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={
            "basis": "v9_contract_mismatch",
            "confirmation": "QUEUE V9 CONTRACT RETESTS",
            "reason": "Restore authoritative v9 evidence after continual ticket reuse",
            "items": [
                {
                    "agent_id": str(agent_id),
                    "request_id": str(uuid4()),
                    "expected_snapshot": item["snapshot"],
                    "expected_run_id": item["run_id"],
                }
            ],
        },
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["queued"] == 1
    assert queued.json()["activated"] == 0

    async with retry_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket,
            (agent_id, 9, "validator-0"),
            with_for_update=True,
        )
        assert ticket is not None
        ticket.status = TicketStatus.EXPIRED
        ticket.retry_after = ticket.deadline + timedelta(hours=6)
        promoted = await activate_next_score_retest(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            supports_version=lambda version: version == 9,
        )
        assert promoted is not None
        assert promoted.agent_id == agent_id
        assert promoted.status == TicketStatus.ISSUED
        assert promoted.purpose == TicketPurpose.CANONICAL_QUORUM
        lifecycle = await session.scalar(
            select(ScoreAuditEntry)
            .where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.validator_hotkey == "validator-0",
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
            )
            .order_by(ScoreAuditEntry.seq.desc())
        )
        assert lifecycle is not None
        assert lifecycle.payload["basis"] == "v9_contract_mismatch"
        assert lifecycle.payload["queued_ticket_status"] == "issued"


async def test_v9_contract_retest_ignores_expired_v8_work_history(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(
        retry_maker,
        score_count=1,
        bench_version=9,
        ticket_count=1,
    )
    await _set_v9_score_contract(
        retry_maker,
        agent_id=agent_id,
        revision="v9-base-shadow-calibration-v1",
        manifest_sha256="5" * 64,
        rollout_mode="shadow",
    )
    async with retry_maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey="legacy-v8-validator",
                status=TicketStatus.EXPIRED,
                issued_at=_T0,
                deadline=_T0 + timedelta(minutes=90),
                bench_version=8,
                attempt_count=MAX_ATTEMPTS_PER_VERSION,
                manual_retry_grants=0,
                retry_after=_T0,
                purpose=TicketPurpose.CONTINUAL_RETEST,
            )
        )
    _install(app, retry_maker)

    preview = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    [item] = preview.json()["items"]
    assert item["agent_id"] == str(agent_id)
    assert item["queue_allowed"] is True

    response = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={
            "basis": "v9_contract_mismatch",
            "confirmation": "QUEUE V9 CONTRACT RETESTS",
            "reason": "Replace v9 evidence without reviving retired v8 work",
            "items": [
                {
                    "agent_id": str(agent_id),
                    "request_id": str(uuid4()),
                    "expected_snapshot": item["snapshot"],
                    "expected_run_id": item["run_id"],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 1
    assert response.json()["skipped"] == 0

    async with retry_maker() as session:
        legacy = await session.get(
            ValidatorTicket,
            (agent_id, 8, "legacy-v8-validator"),
        )
        assert legacy is not None
        assert legacy.status == TicketStatus.EXPIRED


async def test_v9_contract_retests_queue_and_promote_for_evaluating_agents(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_ids = [
        await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
        for _ in range(2)
    ]
    for agent_id in agent_ids:
        await _set_v9_score_contract(
            retry_maker,
            agent_id=agent_id,
            revision="v9-base-shadow-calibration-v1",
            manifest_sha256="5" * 64,
            rollout_mode="shadow",
        )
    _install(app, retry_maker)
    preview = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    by_id = {item["agent_id"]: item for item in preview.json()["items"]}
    request_ids = {agent_id: uuid4() for agent_id in agent_ids}
    response = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={
            "basis": "v9_contract_mismatch",
            "confirmation": "QUEUE V9 CONTRACT RETESTS",
            "reason": (
                "Replace rollout shadow evidence without deleting accepted scores"
            ),
            "items": [
                {
                    "agent_id": str(agent_id),
                    "request_id": str(request_ids[agent_id]),
                    "expected_snapshot": by_id[str(agent_id)]["snapshot"],
                    "expected_run_id": by_id[str(agent_id)]["run_id"],
                }
                for agent_id in agent_ids
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] == 2
    assert [item["queue_position"] for item in response.json()["results"]] == [1, 2]

    async with retry_maker() as session, session.begin():
        promoted = await activate_next_score_retest(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            supports_version=lambda version: version == 9,
        )
        assert promoted is not None and promoted.agent_id == agent_ids[0]
        assert promoted.bench_version == 9
        agent = await session.get(Agent, promoted.agent_id)
        assert agent is not None and agent.status == AgentStatus.EVALUATING
        lifecycle = await session.scalar(
            select(ScoreAuditEntry)
            .where(
                ScoreAuditEntry.agent_id == promoted.agent_id,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
            )
            .order_by(ScoreAuditEntry.seq.desc())
        )
        assert lifecycle is not None
        assert lifecycle.payload["basis"] == "v9_contract_mismatch"
        assert lifecycle.payload["required_v9_contract"]["revision"] == (
            V9_SCORE_CONTRACT_REVISION
        )


async def test_v9_contract_retests_repair_scores_while_source_review_is_pending(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A human hold must not preserve a known non-authoritative score.

    Clearing source review changes the agent directly to ``scored``. If the
    contract-repair lane ignored held agents, an operator could therefore make
    a signed shadow score rankable without ever giving its validator a chance
    to replace it under the enforce contract.
    """
    agent_id = await _seed(retry_maker, score_count=1, bench_version=9, ticket_count=1)
    await _set_v9_score_contract(
        retry_maker,
        agent_id=agent_id,
        revision="v9-base-shadow-calibration-v1",
        manifest_sha256="5" * 64,
        rollout_mode="shadow",
        factor_bps=0,
    )
    async with retry_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.status = AgentStatus.ATH_PENDING_REVIEW
    _install(app, retry_maker)

    preview = await client.get("/api/v1/admin/v9-contract-retests", headers=_HEADERS)
    assert preview.status_code == 200, preview.text
    item = preview.json()["items"][0]
    assert item["agent_id"] == str(agent_id)
    assert item["agent_status"] == AgentStatus.ATH_PENDING_REVIEW
    assert item["observed_rollout_mode"] == "shadow"
    assert item["queue_allowed"] is True

    queued = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={
            "basis": "v9_contract_mismatch",
            "confirmation": "QUEUE V9 CONTRACT RETESTS",
            "reason": "Replace held shadow evidence before source review can clear",
            "items": [
                {
                    "agent_id": str(agent_id),
                    "request_id": str(uuid4()),
                    "expected_snapshot": item["snapshot"],
                    "expected_run_id": item["run_id"],
                }
            ],
        },
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["queued"] == 1

    async with retry_maker() as session, session.begin():
        promoted = await activate_next_score_retest(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            supports_version=lambda version: version == 9,
        )
        assert promoted is not None
        assert promoted.agent_id == agent_id
        assert promoted.bench_version == 9
        held = await session.get(Agent, agent_id)
        assert held is not None
        assert held.status == AgentStatus.ATH_PENDING_REVIEW


async def test_lists_only_unambiguous_finalized_score_outliers(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    low_id = await _seed(retry_maker, score_count=3, composites=[0.12, 0.81, 0.83])
    high_id = await _seed(retry_maker, score_count=3, composites=[0.68, 0.70, 0.96])
    broad_id = await _seed(retry_maker, score_count=3, composites=[0.20, 0.50, 0.80])
    async with retry_maker() as session, session.begin():
        for agent_id in (low_id, high_id, broad_id):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.status = AgentStatus.SCORED
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/score-outliers", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    by_agent = {item["agent_id"]: item for item in body["items"]}
    assert by_agent[str(low_id)]["direction"] == "low"
    assert by_agent[str(low_id)]["outlier"]["validator_hotkey"] == "validator-0"
    assert by_agent[str(high_id)]["direction"] == "high"
    assert by_agent[str(high_id)]["outlier"]["validator_hotkey"] == "validator-2"
    assert str(broad_id) not in by_agent


async def test_score_outliers_cover_only_the_active_benchmark_era(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A previous-era outlier is not an operator's problem any more.

    The only action this page offers is a re-test, and a re-test runs the
    contract the platform scores today. Replaying a submission finalized under
    an older era cannot produce a number comparable with the one it holds, so
    listing it would offer a button that cannot honestly be pressed — and the
    response names the era it scanned so the page cannot imply otherwise.
    """
    current_id = await _seed(retry_maker, score_count=3, composites=[0.12, 0.81, 0.83])
    previous_id = await _seed(
        retry_maker,
        score_count=3,
        composites=[0.14, 0.79, 0.82],
        bench_version=_BENCH_VERSION - 1,
    )
    async with retry_maker() as session, session.begin():
        for agent_id in (current_id, previous_id):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.status = AgentStatus.SCORED
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/score-outliers", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bench_version"] == _BENCH_VERSION
    assert [item["agent_id"] for item in body["items"]] == [str(current_id)]
    assert body["count"] == 1


async def test_score_outlier_scan_reads_the_fleet_in_bulk(
    app: FastAPI,
    retry_engine: AsyncEngine,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Doubling the finalized fleet must not cost a single extra statement.

    The scan used to load each finalized submission on its own — seven round
    trips per agent, including a canonical version that cannot change inside
    one request. At the production fleet size that is thousands of sequential
    queries, and Backroom's score-outlier page gave up before the platform
    answered. Only a statement count catches the regression: a per-agent scan
    returns exactly the same body, just far too late.
    """

    def _record(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    async def _finalize(count: int, composites: list[float]) -> None:
        for _ in range(count):
            agent_id = await _seed(retry_maker, score_count=3, composites=composites)
            async with retry_maker() as session, session.begin():
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                agent.status = AgentStatus.SCORED

    async def _scan() -> tuple[int, int]:
        """Statements the handler runs, and the outliers it reported."""
        statements.clear()
        event.listen(retry_engine.sync_engine, "before_cursor_execute", _record)
        try:
            response = await client.get(
                "/api/v1/admin/score-outliers", headers=_HEADERS
            )
        finally:
            event.remove(retry_engine.sync_engine, "before_cursor_execute", _record)
        assert response.status_code == 200, response.text
        return len(statements), response.json()["count"]

    statements: list[str] = []
    _install(app, retry_maker)
    # One outlier throughout, so the finalized fleet around it is the only
    # thing growing: the per-outlier gate checks are meant to scale with the
    # outliers an operator has to act on, not with every submission ever
    # scored.
    await _finalize(1, [0.12, 0.81, 0.83])
    await _finalize(3, [0.20, 0.50, 0.80])
    small_fleet, small_count = await _scan()
    await _finalize(8, [0.20, 0.50, 0.80])
    large_fleet, large_count = await _scan()

    assert small_count == large_count == 1
    assert large_fleet == small_fleet, (
        f"{small_fleet} statements across 4 finalized agents but {large_fleet} "
        f"across 12: the scan is loading the fleet one submission at a time"
    )


async def _seed_capable_heartbeat(
    maker: async_sessionmaker[AsyncSession], *, hotkey: str
) -> None:
    now = datetime.now(UTC)
    revision = "a" * 40
    components = {
        name: {
            "source_revision": revision if name == "dittobench_api" else "b" * 40,
            "version": "1.3.0" if name == "dittobench_api" else "1.2.0",
            "provenance": "committed_pin",
        }
        for name in (
            "ditto_subnet",
            "dittobench_api",
            "sandbox_docker",
            "model_relay",
            "pylon",
            "ollama",
        )
    }
    async with maker() as session, session.begin():
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=hotkey,
                software_version="1.3.0",
                protocol_version=8,
                code_digest="d" * 64,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                capabilities={
                    "screened_images": True,
                    "require_screened_image": False,
                    "source_build_fallback": True,
                    "full_stack_managed": False,
                    "stack_updater": False,
                    "sandbox_egress_restricted": True,
                    "executor_isolation": "privileged_dind",
                    "scorer_benchmarks": {
                        "status": "fresh_verified",
                        "supported_bench_versions": [_BENCH_VERSION],
                        "observed_at": int(now.timestamp()),
                        "software_version": "1.3.0",
                        "source_revision": revision,
                    },
                },
                stack={
                    "mode": "source",
                    "compose_schema": 1,
                    "release_descriptor_digest": None,
                    "components": components,
                },
            )
        )


async def test_bulk_outlier_queue_waits_behind_validator_and_promotes_serially(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed(retry_maker, score_count=3, composites=[0.12, 0.81, 0.83])
    second = await _seed(retry_maker, score_count=3, composites=[0.18, 0.84, 0.85])
    busy_agent = await _seed(retry_maker)
    async with retry_maker() as session, session.begin():
        for agent_id in (first, second):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.status = AgentStatus.SCORED
        busy = await session.get(
            ValidatorTicket,
            (busy_agent, _BENCH_VERSION, "validator-0"),
        )
        assert busy is not None
        busy.status = TicketStatus.ISSUED
        busy.deadline = datetime.now(UTC) + timedelta(minutes=30)
    await _seed_capable_heartbeat(retry_maker, hotkey="validator-0")
    _install(app, retry_maker)

    outliers = await client.get("/api/v1/admin/score-outliers", headers=_HEADERS)
    by_id = {item["agent_id"]: item for item in outliers.json()["items"]}
    request_ids = {first: uuid4(), second: uuid4()}
    response = await client.post(
        "/api/v1/admin/validation-retries/validators/validator-0/queue-score-retests",
        headers=_HEADERS,
        json={
            "reason": "Shared validator infrastructure failure across these outliers",
            "items": [
                {
                    "agent_id": str(agent_id),
                    "request_id": str(request_ids[agent_id]),
                    "expected_snapshot": by_id[str(agent_id)]["snapshot"],
                    "expected_run_id": "run-0",
                }
                for agent_id in (first, second)
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["activated"] == 0
    assert response.json()["queued"] == 2
    assert [item["queue_position"] for item in response.json()["results"]] == [1, 2]

    async with retry_maker() as session, session.begin():
        legacy_queue_entry = await session.scalar(
            select(ScoreAuditEntry)
            .where(
                ScoreAuditEntry.agent_id == first,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED,
            )
            .order_by(ScoreAuditEntry.seq.desc())
        )
        assert legacy_queue_entry is not None
        assert "basis" not in legacy_queue_entry.payload
        assert "required_v9_contract" not in legacy_queue_entry.payload
        busy = await session.get(
            ValidatorTicket,
            (busy_agent, _BENCH_VERSION, "validator-0"),
        )
        assert busy is not None
        busy.status = TicketStatus.SCORED
        promoted = await activate_next_score_retest(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            supports_version=lambda version: version == _BENCH_VERSION,
        )
        assert promoted is not None and promoted.agent_id == first
        first_request = await session.scalar(
            select(ScoreAuditEntry)
            .where(
                ScoreAuditEntry.agent_id == first,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_REQUESTED,
            )
            .order_by(ScoreAuditEntry.seq.desc())
        )
        assert first_request is not None
        promoted.status = TicketStatus.SCORED
        await append_audit_entry(
            session,
            agent_id=first,
            validator_hotkey="validator-0",
            event=EVENT_SCORE_INVALIDATED,
            payload={"request_id": str(request_ids[first])},
            recorded_at=datetime.now(UTC),
        )
        next_ticket = await activate_next_score_retest(
            session,
            validator_hotkey="validator-0",
            now=datetime.now(UTC),
            supports_version=lambda version: version == _BENCH_VERSION,
        )
        assert next_ticket is not None and next_ticket.agent_id == second
        queued_count = len(
            list(
                (
                    await session.scalars(
                        select(ScoreAuditEntry).where(
                            ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED
                        )
                    )
                ).all()
            )
        )
        assert queued_count == 2


# --- fleet-wide stuck list ------------------------------------------------


async def _seed_states(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    tickets: list[tuple[str, TicketStatus, int, datetime | None]],
    created_offset_hours: float = 24.0,
    agent_status: AgentStatus = AgentStatus.EVALUATING,
    bench_version: int = _BENCH_VERSION,
) -> UUID:
    """Seed one agent with explicit (hotkey, status, attempt_count, retry_after)
    tickets; a SCORED ticket gets a matching score row."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Miner",
                name=name,
                version=1,
                sha256=agent_id.hex * 2,
                status=agent_status,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_T0 - timedelta(hours=created_offset_hours),
            )
        )
        for index, (hotkey, status, attempt, retry_after) in enumerate(tickets):
            deadline = (
                _T0 + timedelta(hours=1)
                if status == TicketStatus.ISSUED
                else _T0 - timedelta(hours=2, minutes=index)
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=hotkey,
                    status=status,
                    issued_at=_T0 - timedelta(hours=3, minutes=index),
                    deadline=deadline,
                    bench_version=bench_version,
                    attempt_count=attempt,
                    manual_retry_grants=0,
                    retry_after=retry_after,
                )
            )
            if status == TicketStatus.SCORED:
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=bench_version,
                        validator_hotkey=hotkey,
                        run_id=f"run-{hotkey}",
                        seed=7,
                        composite=0.7,
                        tool_mean=0.7,
                        memory_mean=0.7,
                        median_ms=100,
                        n=114,
                        generated_at=_T0 - timedelta(hours=1),
                    )
                )
    return agent_id


async def test_list_classifies_every_retry_state(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_states(
        retry_maker,
        name="exhausted-agent",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    cooling_id = await _seed_states(
        retry_maker,
        name="cooling-agent",
        tickets=[("val-0", TicketStatus.EXPIRED, MAX_ATTEMPTS_PER_VERSION, _FUTURE)],
    )
    available_id = await _seed_states(
        retry_maker,
        name="available-agent",
        tickets=[("val-0", TicketStatus.EXPIRED, MAX_ATTEMPTS_PER_VERSION, _PAST)],
    )
    async with retry_maker() as session, session.begin():
        for agent_id in (cooling_id, available_id):
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, "val-0")
            )
            assert ticket is not None
            ticket.infra_retry_grants = 1
    await _seed_states(
        retry_maker,
        name="running-agent",
        tickets=[("val-0", TicketStatus.ISSUED, 1, None)],
    )
    await _seed_states(retry_maker, name="queued-agent", tickets=[])
    await _seed_online_heartbeat(retry_maker, hotkey="val-0")
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {item["agent_name"]: item["retry_state"] for item in body["submissions"]}
    assert by_name == {
        "exhausted-agent": "exhausted",
        "cooling-agent": "queued",
        "available-agent": "queued",
        "running-agent": "running",
        "queued-agent": "queued",
    }
    assert body["counts"] == {
        "exhausted": 1,
        "running": 1,
        "queued": 3,
    }
    assert body["quorum"] == 3


async def test_list_defaults_to_current_generation_but_can_audit_history(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    current = await _seed(retry_maker, ticket_count=3)
    historical = await _seed(
        retry_maker,
        bench_version=_BENCH_VERSION - 1,
        ticket_count=3,
    )
    _install(app, retry_maker)

    active = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    all_generations = await client.get(
        "/api/v1/admin/validation-retries?generation=all", headers=_HEADERS
    )

    assert active.status_code == 200, active.text
    assert active.json()["generation"] == "active"
    assert active.json()["active_bench_version"] == _BENCH_VERSION
    assert {row["agent_id"] for row in active.json()["submissions"]} == {str(current)}
    assert all_generations.status_code == 200, all_generations.text
    assert all_generations.json()["generation"] == "all"
    assert all_generations.json()["active_bench_version"] == _BENCH_VERSION
    assert {row["agent_id"] for row in all_generations.json()["submissions"]} == {
        str(current),
        str(historical),
    }


async def test_list_excludes_agents_at_quorum(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_states(
        retry_maker,
        name="finished-agent",
        tickets=[
            ("val-0", TicketStatus.SCORED, 1, None),
            ("val-1", TicketStatus.SCORED, 1, None),
            ("val-2", TicketStatus.SCORED, 1, None),
        ],
    )
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["submissions"] == []
    assert response.json()["counts"] == {}


async def test_list_state_filter_keeps_fleetwide_counts(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_states(
        retry_maker,
        name="exhausted-agent",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    await _seed_states(retry_maker, name="queued-agent", tickets=[])
    _install(app, retry_maker)

    response = await client.get(
        "/api/v1/admin/validation-retries",
        params={"state": "exhausted"},
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["agent_name"] for item in body["submissions"]] == ["exhausted-agent"]
    # counts stay fleet-wide even though the list is filtered.
    assert body["counts"] == {"exhausted": 1, "queued": 1}


async def test_list_pages_after_stable_triage_sort(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(4):
        await _seed_states(retry_maker, name=f"queued-agent-{index}", tickets=[])
    _install(app, retry_maker)

    complete = await client.get(
        "/api/v1/admin/validation-retries",
        params={"limit": 200},
        headers=_HEADERS,
    )
    page = await client.get(
        "/api/v1/admin/validation-retries",
        params={"limit": 2, "offset": 1},
        headers=_HEADERS,
    )

    assert complete.status_code == 200, complete.text
    assert page.status_code == 200, page.text
    complete_body = complete.json()
    page_body = page.json()
    assert page_body["counts"] == complete_body["counts"]
    assert page_body["quorum"] == complete_body["quorum"]
    assert page_body["count"] == 4
    assert page_body["returned"] == 2
    assert page_body["limit"] == 2
    assert page_body["offset"] == 1
    assert page_body["has_more"] is True
    assert page_body["submissions"] == complete_body["submissions"][1:3]
    assert all("tickets" not in item for item in page_body["submissions"])
    assert all(item["ticket_states"] == {} for item in page_body["submissions"])


async def test_list_rejects_unknown_state(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, retry_maker)
    response = await client.get(
        "/api/v1/admin/validation-retries",
        params={"state": "wedged"},
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert "unknown retry state: wedged" in response.text


async def test_list_snapshot_matches_single_agent_detail(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    # The snapshot an operator reads from the list must drive the per-agent
    # retry endpoint unchanged, so the two routes must agree byte for byte.
    agent_id = await _seed_states(
        retry_maker,
        name="exhausted-agent",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    _install(app, retry_maker)

    listing = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert listing.status_code == 200 and detail.status_code == 200
    item = listing.json()["submissions"][0]
    assert item["snapshot"] == detail.json()["snapshot"]
    assert item["recovery_allowed"] is True
    assert "tickets" not in item
    assert item["ticket_states"] == {"expired": 3}
    assert len(detail.json()["tickets"]) == 3

    accepted = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": item["snapshot"],
            "reason": "chutes infrastructure outage burned the attempt budget",
        },
    )
    assert accepted.status_code == 200, accepted.text


async def test_rollout_cohort_recovery_keeps_settled_status_and_history(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A settled rollout agent recovers even after legacy retry-counter churn."""
    agent_id = uuid4()
    rollout_id = uuid4()
    desired_version = _BENCH_VERSION + 1
    async with retry_maker() as session, session.begin():
        await _activate_current_era(session)
        session.add(
            BenchmarkRollout(
                rollout_id=rollout_id,
                from_version=_BENCH_VERSION,
                desired_version=desired_version,
                status="collecting",
                cohort_size=5,
                rescore_cohort_target=5,
                priority_cohort_target=5,
                created_at=_T0,
            )
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5RolloutMiner",
                name="settled-rollout-member",
                version=1,
                sha256=agent_id.hex * 2,
                status=AgentStatus.SCORED,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_T0 - timedelta(days=1),
            )
        )
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout_id,
                agent_id=agent_id,
                position=1,
                frozen_miner_hotkey="5RolloutMiner",
                frozen_composite=0.9,
            )
        )
        attempts = [47, 48, 61, 38]
        infra_grants = [3, 5, 4, 0]
        for index in range(4):
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=f"rollout-validator-{index}",
                    status=TicketStatus.EXPIRED,
                    issued_at=_T0 - timedelta(hours=3),
                    deadline=_T0 - timedelta(hours=2),
                    bench_version=desired_version,
                    attempt_count=attempts[index],
                    infra_retry_grants=infra_grants[index],
                    retry_after=_PAST,
                )
            )
    _install(app, retry_maker)

    listing = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    item = next(
        row for row in listing.json()["submissions"] if row["agent_id"] == str(agent_id)
    )
    assert item["bench_version"] == desired_version
    assert item["retry_state"] == "exhausted"
    assert item["recovery_allowed"] is True

    response = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": item["snapshot"],
            "reason": "repaired rollout validator infrastructure is serving again",
        },
    )

    assert response.status_code == 200, response.text
    async with retry_maker() as session:
        agent = await session.get(Agent, agent_id)
        tickets = list(
            await session.scalars(
                select(ValidatorTicket)
                .where(ValidatorTicket.agent_id == agent_id)
                .order_by(ValidatorTicket.validator_hotkey)
            )
        )
    assert agent is not None and agent.status == AgentStatus.SCORED
    assert [ticket.manual_retry_grants for ticket in tickets] == [47, 48, 61, 0]
    assert all(
        ticket.attempt_count < ticket_attempt_cap(ticket) for ticket in tickets[:3]
    )
    assert tickets[3].attempt_count >= ticket_attempt_cap(tickets[3])

    async with retry_maker() as session, session.begin():
        rollout = await session.get(BenchmarkRollout, rollout_id)
        assert rollout is not None
        rollout.status = "activated"
        rollout.activated_at = _T0 + timedelta(hours=1)

    closed_detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert closed_detail.status_code == 200
    assert closed_detail.json()["recovery_allowed"] is False
    assert (
        closed_detail.json()["blocking_reason"]
        == "submission is not waiting for validator scores"
    )
    closed_listing = await client.get(
        "/api/v1/admin/validation-retries", headers=_HEADERS
    )
    assert all(
        row["agent_id"] != str(agent_id) for row in closed_listing.json()["submissions"]
    )


async def test_list_sorts_exhausted_before_queued(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    # An older queued agent must still sort behind a newer exhausted one:
    # severity, not age, drives the operator's attention.
    await _seed_states(
        retry_maker, name="old-queued", tickets=[], created_offset_hours=100.0
    )
    await _seed_states(
        retry_maker,
        name="new-exhausted",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
        created_offset_hours=1.0,
    )
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert response.status_code == 200, response.text
    names = [item["agent_name"] for item in response.json()["submissions"]]
    assert names == ["new-exhausted", "old-queued"]


async def test_partial_exhaustion_stays_queued_not_exhausted(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    # One validator burned its budget but two slots were never leased: fresh
    # validators can still reach quorum, so this is queued (self-heals), not
    # exhausted (needs an operator).
    await _seed_states(
        retry_maker,
        name="one-exhausted",
        tickets=[("val-0", TicketStatus.EXPIRED, 2, _PAST)],
    )
    # One accepted score + two exhausted validators: only one slot remains
    # fillable, so a grant IS required → exhausted.
    await _seed_states(
        retry_maker,
        name="two-exhausted-one-scored",
        tickets=[
            ("val-0", TicketStatus.SCORED, 1, None),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    _install(app, retry_maker)

    response = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert response.status_code == 200, response.text
    by_name = {
        item["agent_name"]: item["retry_state"]
        for item in response.json()["submissions"]
    }
    assert by_name["one-exhausted"] == "queued"
    assert by_name["two-exhausted-one-scored"] == "exhausted"


async def test_agent_attributable_exhaustion_refuses_retry_and_recommends_withdraw(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_states(
        retry_maker,
        name="rejected-agent",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    async with retry_maker() as session, session.begin():
        tickets = list(
            await session.scalars(
                select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
            )
        )
        for ticket in tickets:
            ticket.failure_reason = "scoring_error"
            ticket.failure_detail = "inference_request_rejected"
            ticket.failed_at = ticket.issued_at + timedelta(minutes=5)
    _install(app, retry_maker)

    listing = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert listing.status_code == 200 and detail.status_code == 200, listing.text
    item = listing.json()["submissions"][0]
    assert item["retry_state"] == "exhausted"
    assert item["recovery_allowed"] is False
    assert item["recommended_action"] == "withdraw"
    assert item["dominant_failure_code"] == "inference_request_rejected"
    assert item["blocking_reason"] == (
        "exhausted on agent-attributable failures; withdraw rather than retry"
    )
    assert detail.json()["recovery_allowed"] is False
    assert detail.json()["recommended_action"] == "withdraw"
    assert detail.json()["withdrawal_allowed"] is True

    refused = await client.post(
        f"/api/v1/admin/validation-retries/{agent_id}/retry",
        headers=_HEADERS,
        json={
            "request_id": str(uuid4()),
            "expected_snapshot": item["snapshot"],
            "reason": "should not retry an agent-attributable 413/refused run",
        },
    )
    assert refused.status_code == 409
    assert "withdraw rather than retry" in refused.text


async def test_timeout_exhaustion_still_recommends_retry(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_states(
        retry_maker,
        name="timeout-agent",
        tickets=[
            ("val-0", TicketStatus.EXPIRED, 2, _PAST),
            ("val-1", TicketStatus.EXPIRED, 2, _PAST),
            ("val-2", TicketStatus.EXPIRED, 2, _PAST),
        ],
    )
    async with retry_maker() as session, session.begin():
        tickets = list(
            await session.scalars(
                select(ValidatorTicket).where(ValidatorTicket.agent_id == agent_id)
            )
        )
        for ticket in tickets:
            ticket.failure_reason = "scoring_error"
            ticket.failure_detail = (
                "DittobenchError: run deadbeef did not finish within 6600.0s"
            )
            ticket.failed_at = ticket.issued_at + timedelta(minutes=110)
    _install(app, retry_maker)

    listing = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert listing.status_code == 200, listing.text
    item = listing.json()["submissions"][0]
    assert item["retry_state"] == "exhausted"
    assert item["recovery_allowed"] is True
    assert item["recommended_action"] == "retry"
    assert item["dominant_failure_code"] is None


async def test_historical_infra_grant_does_not_authorize_retry(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    # attempt_count sits at the base cap, but an infrastructure grant raised the
    # cap — so the ticket still has budget and reads as retry_available, not
    # exhausted. This is the whole point of infra_retry_grants: an outage does
    # not spend the agent's genuine attempt budget.
    agent_id = uuid4()
    async with retry_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Miner",
                name="infra-compensated",
                version=1,
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_T0 - timedelta(days=1),
            )
        )
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey="val-0",
                status=TicketStatus.EXPIRED,
                issued_at=_T0 - timedelta(hours=3),
                deadline=_T0 - timedelta(hours=2),
                bench_version=_BENCH_VERSION,
                attempt_count=MAX_ATTEMPTS_PER_VERSION,
                manual_retry_grants=0,
                infra_retry_grants=1,
                retry_after=_PAST,
            )
        )
    _install(app, retry_maker)
    await _seed_online_heartbeat(retry_maker, hotkey="val-0")

    response = await client.get("/api/v1/admin/validation-retries", headers=_HEADERS)
    assert response.status_code == 200, response.text
    item = next(
        entry
        for entry in response.json()["submissions"]
        if entry["agent_id"] == str(agent_id)
    )
    assert item["retry_state"] == "queued"
    assert item["ticket_states"] == {"expired": 1}

    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["tickets"][0]["infra_retry_grants"] == 1
    assert detail.json()["tickets"][0]["retry_budget_exhausted"] is True


# --- batch retry-grant ----------------------------------------------------


async def _snapshot_of(client: httpx.AsyncClient, agent_id: UUID) -> str:
    detail = await client.get(
        f"/api/v1/admin/validation-retries/{agent_id}", headers=_HEADERS
    )
    return detail.json()["snapshot"]


async def test_batch_retry_grants_recoverable_and_skips_the_rest(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    recoverable_a = await _seed(retry_maker)
    recoverable_b = await _seed(retry_maker)
    queued = await _seed_states(retry_maker, name="queued", tickets=[])
    stale = await _seed(retry_maker)
    _install(app, retry_maker)

    payload = {
        "reason": "chutes infrastructure outage recovery batch",
        "items": [
            {
                "agent_id": str(recoverable_a),
                "request_id": str(uuid4()),
                "expected_snapshot": await _snapshot_of(client, recoverable_a),
            },
            {
                "agent_id": str(recoverable_b),
                "request_id": str(uuid4()),
                "expected_snapshot": await _snapshot_of(client, recoverable_b),
            },
            {
                "agent_id": str(queued),
                "request_id": str(uuid4()),
                "expected_snapshot": await _snapshot_of(client, queued),
            },
            {
                "agent_id": str(stale),
                "request_id": str(uuid4()),
                "expected_snapshot": "0" * 64,
            },
        ],
    }
    resp = await client.post(
        "/api/v1/admin/validation-retries/batch-retry", headers=_HEADERS, json=payload
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["granted"] == 2
    by_id = {result["agent_id"]: result for result in body["results"]}
    assert by_id[str(recoverable_a)]["status"] == "granted"
    assert by_id[str(recoverable_b)]["status"] == "granted"
    assert by_id[str(queued)]["status"] == "skipped"
    assert (
        by_id[str(queued)]["detail"] == "not enough expired tickets to restore quorum"
    )
    assert by_id[str(stale)]["status"] == "skipped"
    assert by_id[str(stale)]["detail"] == "validation state changed"

    # A granted item actually raised the cap on its tickets.
    async with retry_maker() as session:
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket).where(
                        ValidatorTicket.agent_id == recoverable_a
                    )
                )
            ).all()
        )
        # The gate grants exactly the quorum (3) minimum slots needed.
        assert sum(t.manual_retry_grants for t in tickets) == 3


async def test_batch_retry_is_idempotent(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    payload = {
        "reason": "outage recovery, replayed",
        "items": [
            {
                "agent_id": str(agent_id),
                "request_id": str(uuid4()),
                "expected_snapshot": await _snapshot_of(client, agent_id),
            }
        ],
    }
    first = await client.post(
        "/api/v1/admin/validation-retries/batch-retry", headers=_HEADERS, json=payload
    )
    assert first.json()["results"][0]["status"] == "granted"

    second = await client.post(
        "/api/v1/admin/validation-retries/batch-retry", headers=_HEADERS, json=payload
    )
    assert second.status_code == 200, second.text
    assert second.json()["granted"] == 0
    assert second.json()["results"][0]["status"] == "idempotent"


async def test_batch_retry_rejects_duplicate_agent_ids(
    app: FastAPI,
    client: httpx.AsyncClient,
    retry_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(retry_maker)
    _install(app, retry_maker)
    snapshot = await _snapshot_of(client, agent_id)
    payload = {
        "reason": "duplicate agents in one batch",
        "items": [
            {
                "agent_id": str(agent_id),
                "request_id": str(uuid4()),
                "expected_snapshot": snapshot,
            },
            {
                "agent_id": str(agent_id),
                "request_id": str(uuid4()),
                "expected_snapshot": snapshot,
            },
        ],
    }
    resp = await client.post(
        "/api/v1/admin/validation-retries/batch-retry", headers=_HEADERS, json=payload
    )
    assert resp.status_code == 422
