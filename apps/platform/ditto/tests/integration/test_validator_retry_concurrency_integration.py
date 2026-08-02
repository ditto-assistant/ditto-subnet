"""Real-Postgres row-lock proof for validator retry recovery."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_models.admin_validation_retry import AdminValidationRetryRequest
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.bench import CURRENT_BENCH_VERSION
from ditto.api_server.endpoints.admin_validation_retry import (
    get_validation_retry,
    retry_validation_after_infrastructure_failure,
)
from ditto.db import create_db_engine
from ditto.db.models import Agent, ValidatorRetryRecovery, ValidatorTicket
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION

pytestmark = pytest.mark.integration

_AGENT = UUID("00000000-0000-0000-0000-000000000301")
_NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)


async def test_concurrent_recoveries_have_one_winner() -> None:
    engine = create_db_engine()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE agents CASCADE"))
        session.add(
            Agent(
                agent_id=_AGENT,
                miner_hotkey="5ConcurrentMiner",
                name="concurrent-retry",
                sha256=_AGENT.hex * 2,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        for index in range(4):
            session.add(
                ValidatorTicket(
                    agent_id=_AGENT,
                    validator_hotkey=f"validator-{index}",
                    status=TicketStatus.EXPIRED,
                    issued_at=_NOW - timedelta(hours=2),
                    deadline=_NOW - timedelta(hours=1, minutes=index),
                    bench_version=CURRENT_BENCH_VERSION,
                    attempt_count=MAX_ATTEMPTS_PER_VERSION,
                    retry_after=_NOW - timedelta(minutes=30),
                )
            )

    async with maker() as session:
        detail = await get_validation_retry(_AGENT, None, session)

    async def recover(label: str) -> object:
        async with maker() as session:
            try:
                return await retry_validation_after_infrastructure_failure(
                    _AGENT,
                    AdminValidationRetryRequest(
                        request_id=uuid4(),
                        expected_snapshot=detail.snapshot,
                        reason=f"Verified infrastructure failure {label}",
                    ),
                    None,
                    session,
                    "operator",
                )
            except HTTPException as exc:
                return exc

    outcomes = await asyncio.gather(recover("one"), recover("two"))
    assert sorted(
        outcome.status_code if isinstance(outcome, HTTPException) else 200
        for outcome in outcomes
    ) == [200, 409]

    async with maker() as session:
        recovery_count = await session.scalar(
            select(func.count()).select_from(ValidatorRetryRecovery)
        )
        tickets = list(
            (
                await session.scalars(
                    select(ValidatorTicket)
                    .where(ValidatorTicket.agent_id == _AGENT)
                    .order_by(ValidatorTicket.validator_hotkey)
                )
            ).all()
        )
    assert recovery_count == 1
    assert sorted(ticket.manual_retry_grants for ticket in tickets) == [0, 1, 1, 1]
    await engine.dispose()
