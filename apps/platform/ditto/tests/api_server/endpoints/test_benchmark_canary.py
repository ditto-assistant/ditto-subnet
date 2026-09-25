"""Real-Postgres canary authority, transport and score-isolation regressions."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import bittensor
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import ScoreReport
from ditto.api_server.endpoints import admin_benchmark_canary, validator
from ditto.api_server.private_benchmark_preparation import (
    PrivatePreparationConfig,
)
from ditto.db.models import (
    Agent,
    BenchmarkCanary,
    BenchmarkRollout,
    PrivateBenchmarkPreparation,
    Score,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.retry_state import resolve_bench_version
from ditto.db.queries.tickets import expire_overdue_tickets, ticket_retry_budget_spent
from ditto.tests.api_server.endpoints.test_admin_benchmark_rollout import (
    _HEADERS,
    _add_cohort_agent,
    _add_ready_route,
    _capabilities,
    _install,
    _StubGenerator,
)

pytestmark = pytest.mark.asyncio
KEY = bittensor.Keypair.create_from_uri("//Alice")
HOTKEY = KEY.ss58_address


@pytest.fixture
async def ready(app, session_maker, monkeypatch):
    _install(app, session_maker)
    app.state.session_maker = session_maker
    app.state.private_preparation_sessions = None
    app.state.chain = MagicMock()
    app.state.dataset_generator = _StubGenerator()
    app.state.config = replace(
        app.state.config,
        private_preparation=PrivatePreparationConfig(),
    )
    monkeypatch.setattr(
        admin_benchmark_canary, "_assert_validator_permitted", AsyncMock()
    )
    monkeypatch.setattr(validator, "_assert_validator_permitted", AsyncMock())
    now = datetime.now(UTC)
    capabilities, stack = _capabilities(now)
    capabilities["scorer_benchmarks"]["supported_bench_versions"] = [7, 8, 12, 13]
    capabilities["scorer_benchmarks"]["deterministic_v13_datasets"] = True
    async with session_maker() as session, session.begin():
        member = _add_cohort_agent(
            session, position=1, composite=0.5, now=now, bench_version=7
        )
        await session.flush()
        agent = await session.get(Agent, member.agent_id)
        agent.dataset_seed_block = 100
        agent.dataset_seed_block_hash = "ab" * 32
        _add_ready_route(session, now)
        session.add(
            ValidatorHeartbeat(
                validator_hotkey=HOTKEY,
                software_version="1.3.0",
                protocol_version=12,
                code_digest="d" * 64,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                capabilities=capabilities,
                stack=stack,
                benchmark_capacity={
                    "configured_slots": 2,
                    "healthy_slots": ["slot-0", "slot-1"],
                    "admission": "accepting",
                    "active": [],
                },
            )
        )
        active = await active_bench_version(session)
    return {
        "canary_id": str(uuid4()),
        "agent_id": str(member.agent_id),
        "bench_version": 13,
        "validator_hotkey": HOTKEY,
        "slot_id": "slot-0",
        "expected_artifact_sha256": "1" * 64,
        "expected_screened_image_sha256": "1" * 64,
        "expected_active_version": active,
        "actor": "operator@example.com",
        "reason": "One explicitly non-authoritative V13 diagnostic",
        "confirmation": f"ISSUE CANARY V13 {member.agent_id}",
    }


async def issue(client, payload):
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_v13_canary_ignores_deferred_private_configuration(
    client, ready, app, session_maker
):
    app.state.config = replace(
        app.state.config,
        private_preparation=replace(
            app.state.config.private_preparation, profile_sha256="a" * 64
        ),
    )
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
    )
    assert response.status_code == 200, response.text
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PrivateBenchmarkPreparation)
            )
            == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkCanary)) == 1
        )


async def test_concurrent_issue_is_one_lease(client, ready, session_maker):
    responses = await asyncio.gather(
        *[
            client.post(
                "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
            )
            for _ in range(2)
        ]
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkCanary)) == 1
        )


@pytest.mark.parametrize("field,value", [("seed", 0), ("dataset_sha256", "f" * 64)])
async def test_wrong_report_pin_cannot_finish(
    client, ready, session_maker, field, value
):
    from fastapi import HTTPException

    from ditto.db.queries.benchmark_canaries import finish_canary

    row = await issue(client, ready)
    report = ScoreReport(
        run_id="wrong",
        bench_version=13,
        seed=value if field == "seed" else int(row["seed"]),
        composite=0.5,
        tool_mean=0.5,
        memory_mean=0.5,
        median_ms=1,
        n=351,
        generated_at=datetime.now(UTC),
        details={
            "dataset_sha256": value
            if field == "dataset_sha256"
            else row["dataset_sha256"]
        },
    )
    async with session_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        canary = await session.get(BenchmarkCanary, UUID(row["canary_id"]))
        with pytest.raises(HTTPException, match="pinned dataset"):
            await finish_canary(
                session,
                canary=canary,
                ticket=ticket,
                now=datetime.now(UTC),
                report=report,
            )
        assert canary.status == "issued"
        assert await counts(session) == (3, 0)


async def test_issue_uses_dispatch_lock_order(client, ready, monkeypatch):
    calls = []
    gate = admin_benchmark_canary.lock_provider_work_gate
    slot = admin_benchmark_canary.lock_validator_slot

    async def record_gate(*args, **kwargs):
        calls.append("provider")
        return await gate(*args, **kwargs)

    async def record_slot(*args, **kwargs):
        calls.append("slot")
        return await slot(*args, **kwargs)

    monkeypatch.setattr(admin_benchmark_canary, "lock_provider_work_gate", record_gate)
    monkeypatch.setattr(admin_benchmark_canary, "lock_validator_slot", record_slot)
    await issue(client, ready)
    assert calls == ["provider", "slot"]


async def counts(session):
    return tuple(
        [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Score, BenchmarkRollout)
        ]
    )


async def test_signed_failure_is_not_miner_fault(client, ready, session_maker):
    from ditto.tests.api_server.endpoints.test_validator import _job_fail_payload

    row = await issue(client, ready)
    response = await client.post(
        "/api/v1/validator/job/fail",
        headers={"X-Validator-Hotkey": HOTKEY},
        json=_job_fail_payload(
            UUID(ready["agent_id"]),
            KEY,
            reason="scoring_error",
            ticket_deadline=datetime.fromisoformat(
                row["deadline"].replace("Z", "+00:00")
            ),
        ),
    )
    assert response.status_code == 200, response.text
    async with session_maker() as session:
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.failure_reason is None
        assert not ticket_retry_budget_spent(ticket)
        assert (
            await session.get(BenchmarkCanary, UUID(row["canary_id"]))
        ).status == "failed"
        assert await counts(session) == (3, 0)


@pytest.mark.parametrize("mode", ["stale", "paused", "occupied", "malformed"])
async def test_heartbeat_admission_guards(client, ready, session_maker, mode):
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, HOTKEY)
        if mode == "stale":
            heartbeat.seen_at = datetime.now(UTC) - timedelta(days=1)
        else:
            capacity = dict(heartbeat.benchmark_capacity)
            if mode == "paused":
                capacity["admission"] = "draining"
            elif mode == "occupied":
                capacity["healthy_slots"] = []
            else:
                capacity["configured_slots"] = "invalid"
            heartbeat.benchmark_capacity = capacity
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
    )
    assert response.status_code == 409, response.text
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkCanary)) == 0
        )


async def test_generator_failure_rolls_back_lease(client, ready, app, session_maker):
    app.state.dataset_generator.generate = AsyncMock(
        side_effect=HTTPException(503, "deterministic generation unavailable")
    )
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
    )
    assert response.status_code == 503, response.text
    async with session_maker() as session:
        assert (
            await session.get(ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY))
            is None
        )
        assert await counts(session) == (3, 0)


async def test_deterministic_canary_never_enqueues_private_preparation(
    client, ready, app, session_maker
):
    app.state.config = replace(
        app.state.config,
        private_preparation=PrivatePreparationConfig("b" * 64),
    )
    public = AsyncMock(return_value="c" * 64)
    app.state.dataset_generator.generate = public
    for _ in range(2):
        response = await client.post(
            "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
        )
        assert response.status_code == 200, response.text
    public.assert_awaited_once()
    async with session_maker() as session:
        assert (
            await session.get(ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY))
            is not None
        )
        assert (
            await session.scalar(select(func.count()).select_from(BenchmarkCanary)) == 1
        )
        rows = list(await session.scalars(select(PrivateBenchmarkPreparation)))
        assert rows == []


async def test_old_v13_validator_cannot_enqueue(client, ready, session_maker):
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(ValidatorHeartbeat, HOTKEY)
        capabilities = dict(heartbeat.capabilities)
        capabilities["scorer_benchmarks"] = {
            **capabilities["scorer_benchmarks"],
            "deterministic_v13_datasets": False,
        }
        heartbeat.capabilities = capabilities
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=ready
    )
    assert response.status_code == 409, response.text
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PrivateBenchmarkPreparation)
            )
            == 0
        )


async def test_later_ordinary_lease_starts_fresh(client, ready, session_maker):
    from ditto.api_models.agent_status import AgentStatus
    from ditto.api_models.screener import SCREENING_POLICY_VERSION
    from ditto.db.models import BenchmarkDataset
    from ditto.db.queries.tickets import issue_ticket

    row = await issue(client, ready)
    await client.post(
        f"/api/v1/admin/benchmark-canaries/{row['canary_id']}/cancel",
        headers=_HEADERS,
        json={
            "actor": ready["actor"],
            "reason": "End isolated diagnostic",
            "confirmation": f"CANCEL CANARY {row['canary_id']}",
        },
    )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, UUID(ready["agent_id"]))
        agent.status = AgentStatus.EVALUATING
        agent.screening_policy_version = SCREENING_POLICY_VERSION
        session.add(
            BenchmarkDataset(
                agent_id=agent.agent_id,
                bench_version=13,
                seed=1,
                sha256="d" * 64,
                run_size="full",
            )
        )
        await session.flush()
        ticket = await issue_ticket(
            session,
            validator_hotkey=HOTKEY,
            now=datetime.now(UTC),
            ttl=timedelta(hours=3),
            bench_version=13,
            artifact_mode="screened_only",
        )
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CANONICAL_QUORUM
        assert ticket.attempt_count == 1
        assert ticket.seed is None
        assert ticket.dataset_sha256 is None
        assert ticket.deadline.isoformat() != row["deadline"]
        assert (
            await session.get(BenchmarkCanary, UUID(row["canary_id"]))
        ).status == "cancelled"


async def test_idle_revocation_finishes_canary(client, ready, session_maker):
    from ditto.db.queries.lease_liveness import LeaseLiveness, force_expire_lease

    row = await issue(client, ready)
    async with session_maker() as session, session.begin():
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        original_deadline = ticket.deadline
        await force_expire_lease(
            session,
            ticket=ticket,
            now=datetime.now(UTC),
            liveness=LeaseLiveness(idle=True, reason="test_idle"),
            context="canary-test",
        )
        assert ticket.deadline == original_deadline
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.retry_after is None
        assert (
            await session.get(BenchmarkCanary, UUID(row["canary_id"]))
        ).status == "failed"


async def test_expiry_is_terminal_without_retry_cost(client, ready, session_maker):
    row = await issue(client, ready)
    async with session_maker() as session, session.begin():
        await expire_overdue_tickets(session, now=datetime.now(UTC) + timedelta(days=1))
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.retry_after is None
        assert not ticket_retry_budget_spent(ticket)
    response = await client.post(
        f"/api/v1/admin/benchmark-canaries/{row['canary_id']}/cancel",
        headers=_HEADERS,
        json={
            "actor": ready["actor"],
            "reason": "Wrong confirmation refuses mutation",
            "confirmation": "yes",
        },
    )
    assert response.status_code == 422


async def test_issue_idempotent_and_no_score_or_rollout_mutation(
    client, ready, session_maker
):
    async with session_maker() as session:
        before = await counts(session)
    row = await issue(client, ready)
    assert row["authoritative"] is False
    assert row["bench_version"] == 13
    assert isinstance(row["seed"], str)
    assert await issue(client, ready) == row
    async with session_maker() as session:
        assert await counts(session) == before
        assert await active_bench_version(session) == ready["expected_active_version"]
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        assert ticket.purpose == TicketPurpose.BENCHMARK_CANARY
        assert (
            await session.get(Agent, UUID(ready["agent_id"]))
        ).status.value == "scored"
    conflict = await client.post(
        "/api/v1/admin/benchmark-canaries",
        headers=_HEADERS,
        json={**ready, "reason": "Different intent for same ID"},
    )
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("confirmation", "START BENCHMARK V13", 422),
        ("bench_version", 999, 422),
        ("expected_artifact_sha256", "f" * 64, 409),
        ("expected_active_version", 999, 409),
        ("slot_id", "slot-7", 409),
    ],
)
async def test_issue_guards_fail_closed(client, ready, field, value, status):
    payload = {**ready, field: value}
    if field == "bench_version":
        payload["confirmation"] = f"ISSUE CANARY V{value} {ready['agent_id']}"
    response = await client.post(
        "/api/v1/admin/benchmark-canaries", headers=_HEADERS, json=payload
    )
    assert response.status_code == status, response.text


async def test_auth_and_singleton(client, ready):
    response = await client.post("/api/v1/admin/benchmark-canaries", json=ready)
    assert response.status_code == 401
    await issue(client, ready)
    response = await client.post(
        "/api/v1/admin/benchmark-canaries",
        headers=_HEADERS,
        json={**ready, "canary_id": str(uuid4()), "slot_id": "slot-1"},
    )
    assert response.status_code == 409
    assert "another canary" in response.text


async def test_signed_claim_and_result_are_diagnostic_only(
    client, ready, session_maker
):
    row = await issue(client, ready)
    now, nonce = datetime.now(UTC), uuid4()
    signature = KEY.sign(
        validator._job_signing_message(HOTKEY, nonce, now, "slot-0")
    ).hex()
    claim = await client.post(
        "/api/v1/validator/job",
        headers={"X-Validator-Hotkey": HOTKEY},
        json={
            "validator_hotkey": HOTKEY,
            "nonce": str(nonce),
            "requested_at": now.isoformat(),
            "signature": signature,
            "slot_id": "slot-0",
        },
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["bench_version"] == 13
    assert claim.json()["dataset_sha256"] == row["dataset_sha256"]
    report = ScoreReport(
        run_id="canary-run",
        bench_version=13,
        seed=int(row["seed"]),
        composite=0.5,
        tool_mean=0.5,
        memory_mean=0.5,
        median_ms=1,
        n=350,
        generated_at=now,
        details={"dataset_sha256": row["dataset_sha256"]},
    )
    deadline = datetime.fromisoformat(row["deadline"].replace("Z", "+00:00"))
    signature = KEY.sign(
        validator._score_signing_message(
            HOTKEY, UUID(ready["agent_id"]), deadline, report
        )
    ).hex()
    payload = {
        "validator_hotkey": HOTKEY,
        "ticket_deadline": row["deadline"],
        "signature": signature,
        "report": report.model_dump(mode="json"),
    }
    path = f"/api/v1/validator/agent/{ready['agent_id']}/score"
    for _ in range(2):
        result = await client.post(path, json=payload)
        assert result.status_code == 200, result.text
    async with session_maker() as session:
        assert await counts(session) == (3, 0)
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.failure_reason is None
        assert not ticket_retry_budget_spent(ticket)
        assert (
            resolve_bench_version(
                all_tickets=[ticket], all_scores=[], canonical_version=12
            )
            == 12
        )
        canary = await session.get(BenchmarkCanary, UUID(row["canary_id"]))
        assert canary.status == "completed"
        assert canary.result["run_id"] == "canary-run"


async def test_db_rejects_accidental_authoritative_write(client, ready, session_maker):
    row = await issue(client, ready)
    async with session_maker() as session:
        session.add(
            Score(
                agent_id=UUID(ready["agent_id"]),
                bench_version=13,
                validator_hotkey=HOTKEY,
                run_id="must-not-rank",
                signature="aa",
                seed=int(row["seed"]),
                composite=0.5,
                tool_mean=0.5,
                memory_mean=0.5,
                median_ms=1,
                n=350,
                generated_at=datetime.now(UTC),
            )
        )
        with pytest.raises(DBAPIError, match="canary cannot write"):
            await session.commit()


async def test_cancel_and_expiry_do_not_spend_miner_budget(
    client, ready, session_maker
):
    row = await issue(client, ready)
    path = f"/api/v1/admin/benchmark-canaries/{row['canary_id']}/cancel"
    response = await client.post(
        path,
        headers=_HEADERS,
        json={
            "actor": ready["actor"],
            "reason": "Operator stops diagnostic run",
            "confirmation": f"CANCEL CANARY {row['canary_id']}",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    async with session_maker() as session, session.begin():
        await expire_overdue_tickets(session, now=datetime.now(UTC) + timedelta(days=1))
        ticket = await session.get(
            ValidatorTicket, (UUID(ready["agent_id"]), 13, HOTKEY)
        )
        assert not ticket_retry_budget_spent(ticket)
        assert ticket.retry_after is None
        assert await counts(session) == (3, 0)
