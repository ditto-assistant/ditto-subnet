"""The SN-485 per-agent cap on free within-era re-leases.

Counts only recorded ticket outcomes, never caps platform-caused re-tests
(``dueling_season_rotation``) or fleet outages, never undoes an operator's
reinstatement, and does nothing while the readmission fee is inactive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import Agent, ValidatorQueueWithdrawal, ValidatorTicket
from ditto.db.queries import free_retry_cap
from ditto.db.queries.audit import EVENT_SCORE_RETEST_QUEUED, append_audit_entry
from ditto.db.queries.free_retry_cap import (
    FREE_RETRY_CAP_ACTOR,
    FREE_RETRY_CAP_FAILED_LEASES,
    free_retry_cap_candidates,
    sweep_free_retry_cap,
    withdraw_at_free_retry_cap,
)
from ditto.db.queries.score_retests import DUELING_ROTATION_RETEST_BASIS

pytestmark = pytest.mark.asyncio

_VERSION = 7
_NOW = datetime.now(UTC)


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    failures: int = FREE_RETRY_CAP_FAILED_LEASES,
    detail: str | None = None,
    rotation_retests: int = 0,
) -> UUID:
    """An evaluating agent whose ``failures`` leases all expired unscored."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner-{agent_id.hex[:8]}",
                name=f"capped-{agent_id.hex[:8]}",
                version=1,
                sha256=f"{agent_id.int:064x}",
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
                created_at=_NOW - timedelta(days=1),
            )
        )
        await session.flush()
        for index in range(failures):
            failed = _NOW - timedelta(hours=1, minutes=index)
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=f"5Validator-{index}",
                    status=TicketStatus.EXPIRED,
                    issued_at=failed - timedelta(minutes=30),
                    deadline=failed,
                    bench_version=_VERSION,
                    attempt_count=1,
                    manual_retry_grants=0,
                    infra_retry_grants=0,
                    failure_reason="scoring_error" if detail else None,
                    failure_detail=detail,
                    failed_at=failed if detail else None,
                    retry_after=failed,
                )
            )
        await session.flush()
        for index in range(rotation_retests):
            await append_audit_entry(
                session,
                agent_id=agent_id,
                validator_hotkey=f"5Validator-{index}",
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={
                    "request_id": str(uuid4()),
                    "actor": "platform:dueling-season-catchup",
                    "reason": "dueling season catch-up",
                    "basis": DUELING_ROTATION_RETEST_BASIS,
                    "bench_version": _VERSION,
                    "catch_up": True,
                    "catch_up_source": "off_board",
                    "automatic": True,
                },
                recorded_at=_NOW,
            )
    return agent_id


async def _cap(maker: async_sessionmaker[AsyncSession], agent_id: UUID):  # type: ignore[no-untyped-def]
    async with maker() as session, session.begin():
        return await withdraw_at_free_retry_cap(
            session, agent_id=agent_id, bench_version=_VERSION, now=_NOW
        )


async def test_an_agent_failing_on_every_free_lease_is_withdrawn_once(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker)
    withdrawal = await _cap(session_maker, agent_id)
    assert withdrawal is not None
    assert withdrawal.actor == FREE_RETRY_CAP_ACTOR
    # An ordinary withdrawal: public surfaces never see an unknown code.
    assert withdrawal.terminal_failure_code is None
    assert len(withdrawal.ticket_snapshot) == FREE_RETRY_CAP_FAILED_LEASES
    assert await _cap(session_maker, agent_id) is None


async def test_below_the_cap_nothing_happens(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker, failures=FREE_RETRY_CAP_FAILED_LEASES - 1)
    assert await _cap(session_maker, agent_id) is None


async def test_dueling_season_rotation_retests_are_never_capped(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """SN-485 section 3 and SN-486: platform-caused re-tests stay free.

    Season rotation, budgeted catch-up overflow, off-board catch-up and the
    ATH-clear re-test are all ``dueling_season_rotation`` score re-tests; a
    lease on a validator that ran one never counts toward the cap.
    """
    agent_id = await _seed(session_maker, rotation_retests=1)
    async with session_maker() as session:
        assert agent_id not in await free_retry_cap_candidates(
            session, bench_version=_VERSION
        )
    assert await _cap(session_maker, agent_id) is None


async def test_fleet_outages_are_never_capped(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker, detail="provider_outage_parked")
    assert await _cap(session_maker, agent_id) is None


async def test_an_operator_reinstatement_is_never_undone(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed(session_maker)
    assert await _cap(session_maker, agent_id) is not None
    async with session_maker() as session, session.begin():
        row = await session.scalar(
            select(ValidatorQueueWithdrawal).where(
                ValidatorQueueWithdrawal.agent_id == agent_id
            )
        )
        assert row is not None
        row.reinstated_at = _NOW
    assert await _cap(session_maker, agent_id) is None


async def test_the_sweep_is_inert_until_readmission_is_active(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _seed(session_maker)
    monkeypatch.setattr(
        free_retry_cap, "active_bench_version", AsyncMock(return_value=_VERSION)
    )
    assert await sweep_free_retry_cap(session_maker) == 0

    monkeypatch.setattr(
        free_retry_cap,
        "current_readmission_activation",
        AsyncMock(return_value=SimpleNamespace(active=True)),
    )
    assert await sweep_free_retry_cap(session_maker) >= 1
    async with session_maker() as session:
        assert agent_id not in await free_retry_cap_candidates(
            session, bench_version=_VERSION
        )


async def test_the_sweep_stops_when_the_gate_closes_mid_sweep(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the announcement stops the sweep before the next withdrawal."""
    first = await _seed(session_maker)
    second = await _seed(session_maker)
    monkeypatch.setattr(
        free_retry_cap, "active_bench_version", AsyncMock(return_value=_VERSION)
    )
    # Active for the candidate read, then closed before the first withdrawal.
    monkeypatch.setattr(
        free_retry_cap,
        "current_readmission_activation",
        AsyncMock(
            side_effect=[
                SimpleNamespace(active=True),
                SimpleNamespace(active=False),
            ]
        ),
    )
    assert await sweep_free_retry_cap(session_maker) == 0
    async with session_maker() as session:
        still = await free_retry_cap_candidates(session, bench_version=_VERSION)
    assert first in still and second in still
