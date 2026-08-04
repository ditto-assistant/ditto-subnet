"""Real-Postgres concurrency coverage for signed validator heartbeats."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import BenchmarkCapacity
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_models.validator import ValidatorHeartbeatRequest
from ditto.api_server.endpoints.validator import _validated_heartbeat_work
from ditto.db import create_db_engine, create_session_maker
from ditto.db.models import Agent, ValidatorHeartbeat, ValidatorTicket
from ditto.db.queries.heartbeats import upsert_validator_heartbeat

pytestmark = pytest.mark.integration

_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
# Arbitrary but module-private; advisory-lock keys share one global namespace.
_MODULE_LOCK_KEY = 0x_D177_0001


@pytest.fixture(scope="module", autouse=True)
def _alembic_upgrade_head() -> None:
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
    )


@pytest.fixture
async def session_maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_db_engine()
    # Every test here truncates the same two tables against one shared database,
    # so two of them landing on different xdist workers wipe each other's
    # fixtures mid-run. The session-level advisory lock is held for the whole
    # test — truncate included — which serializes this module without
    # serializing the suite. ``--dist=worksteal`` ignores ``xdist_group``, so the
    # lock has to live in the database rather than in the scheduler.
    guard = await engine.connect()
    try:
        await guard.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": _MODULE_LOCK_KEY}
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE validator_heartbeats, agents CASCADE")
            )
        yield create_session_maker(engine)
    finally:
        # Explicit: returning the connection to the pool rolls back but does not
        # drop a session-level advisory lock.
        await guard.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": _MODULE_LOCK_KEY}
        )
        await guard.close()
        await engine.dispose()


async def _upsert_idle(
    session: AsyncSession, *, reported_at: datetime
) -> tuple[ValidatorHeartbeat, bool]:
    return await upsert_validator_heartbeat(
        session,
        validator_hotkey=_HOTKEY,
        software_version="1.2.3",
        protocol_version=4,
        code_digest="ab" * 32,
        state="idle",
        active_agent_id=None,
        system_metrics=None,
        benchmark_progress=None,
        reported_at=reported_at,
        seen_at=reported_at,
        signature="cd" * 64,
    )


async def test_concurrent_first_heartbeat_uses_on_conflict_loser_path(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Two missing-row writers serialize without a PK error or lost update."""
    first_at = datetime.now(UTC)
    second_at = first_at + timedelta(seconds=1)
    second_started = asyncio.Event()

    async def second_writer() -> tuple[ValidatorHeartbeat, bool]:
        async with session_maker() as session, session.begin():
            second_started.set()
            return await _upsert_idle(session, reported_at=second_at)

    async with session_maker() as first_session:
        transaction = await first_session.begin()
        _, first_accepted = await _upsert_idle(first_session, reported_at=first_at)
        assert first_accepted is True

        second_task = asyncio.create_task(second_writer())
        await second_started.wait()
        await asyncio.sleep(0.05)
        assert not second_task.done(), "second INSERT should wait on the PK conflict"
        await transaction.commit()

    second_row, second_accepted = await asyncio.wait_for(second_task, timeout=2)
    assert second_accepted is True
    assert second_row.reported_at == second_at

    async with session_maker() as session:
        count = await session.scalar(
            select(func.count()).select_from(ValidatorHeartbeat)
        )
        row = await session.get(ValidatorHeartbeat, _HOTKEY)
    assert count == 1
    assert row is not None and row.reported_at == second_at


async def test_capacity_progress_does_not_wait_behind_ticket_accounting_lock(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Inference accounting cannot delay the validator-wide progress snapshot.

    Every proxied inference request briefly locks its ticket while reserving or
    finalizing budget.  A chat-heavy benchmark may therefore keep this row busy
    nearly continuously.  Heartbeat validation is observational and must retain
    the live slot without joining that queue; only its optional first-report
    stamp may lock, and that write skips a busy row and retries next heartbeat.
    """
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=30)
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                name="busy-inference-agent",
                sha256="ab" * 32,
                size_bytes=524288,
                status=AgentStatus.EVALUATING,
                created_at=now,
            )
        )
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                validator_hotkey=_HOTKEY,
                status=TicketStatus.ISSUED,
                issued_at=now,
                deadline=deadline,
                bench_version=8,
                slot_id="slot-0",
            )
        )

    capacity = BenchmarkCapacity.model_validate(
        {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [
                {
                    "slot_id": "slot-0",
                    "agent_id": str(agent_id),
                    "bench_version": 8,
                    "progress": None,
                }
            ],
        }
    )
    heartbeat = ValidatorHeartbeatRequest.model_construct(
        validator_hotkey=_HOTKEY,
        software_version="0.44.1",
        protocol_version=18,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=agent_id,
        system_metrics=None,
        benchmark_progress=None,
        capabilities=None,
        stack=None,
        stack_health=None,
        benchmark_capacity=capacity,
        timestamp=int(now.timestamp()),
        signature="cd" * 64,
    )

    async def validate_while_busy():
        async with session_maker() as session, session.begin():
            return await _validated_heartbeat_work(
                session,
                validator_hotkey=_HOTKEY,
                request_body=heartbeat,
                now=now,
            )

    async with session_maker() as accounting_session:
        transaction = await accounting_session.begin()
        locked = await accounting_session.scalar(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.bench_version == 8,
                ValidatorTicket.validator_hotkey == _HOTKEY,
            )
            .with_for_update()
        )
        assert locked is not None

        work = await asyncio.wait_for(validate_while_busy(), timeout=1)
        assert work.benchmark_capacity is not None
        assert [slot.agent_id for slot in work.benchmark_capacity.active] == [agent_id]
        assert locked.first_reported_at is None
        await transaction.rollback()

    # The skipped stamp is best-effort, not lost.  Once accounting releases the
    # row, the next heartbeat records that this lease has testified.
    await validate_while_busy()
    async with session_maker() as session:
        ticket = await session.get(ValidatorTicket, (agent_id, 8, _HOTKEY))
    assert ticket is not None and ticket.first_reported_at is not None
