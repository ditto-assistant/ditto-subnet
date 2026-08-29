from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import Agent, ProviderOutageCircuit, ValidatorTicket
from ditto.db.queries.provider_outages import (
    lock_provider_work_gate,
    park_scoring_leases,
    register_provider_probe,
)

_NOW = datetime(2026, 8, 28, 18, 0, tzinfo=UTC)


async def test_open_circuit_stops_work_then_admits_one_half_open_probe(
    session: AsyncSession,
) -> None:
    epoch = uuid4()
    async with session.begin():
        session.add(
            ProviderOutageCircuit(
                provider="openrouter",
                state="open",
                epoch=epoch,
                opened_at=_NOW,
                retry_at=_NOW + timedelta(minutes=2),
                last_failure_at=_NOW,
                failure_count=1,
                last_status=429,
                last_error_code="upstream_http_429",
                updated_at=_NOW,
            )
        )

    async with session.begin():
        cooling = await lock_provider_work_gate(
            session, now=_NOW, kind="scoring", key="validator-a:slot-0"
        )
        assert cooling.admitted is False

    half_open_at = _NOW + timedelta(minutes=3)
    async with session.begin():
        first = await lock_provider_work_gate(
            session,
            now=half_open_at,
            kind="scoring",
            key="validator-a:slot-0",
        )
        assert first.admitted is True
        assert first.probe_required is True
        register_provider_probe(
            first,
            now=half_open_at,
            kind="scoring",
            key="validator-a:slot-0",
        )

    async with session.begin():
        sibling = await lock_provider_work_gate(
            session,
            now=half_open_at,
            kind="screening",
            key=str(uuid4()),
        )
        assert sibling.admitted is False
        resume = await lock_provider_work_gate(
            session,
            now=half_open_at,
            kind="scoring",
            key="validator-a:slot-0",
        )
        assert resume.admitted is True
        assert resume.probe_required is False


async def test_closed_circuit_has_unlimited_capacity(session: AsyncSession) -> None:
    async with session.begin():
        session.add(
            ProviderOutageCircuit(
                provider="openrouter",
                state="closed",
                epoch=uuid4(),
                opened_at=_NOW,
                retry_at=_NOW,
                last_failure_at=_NOW,
                closed_at=_NOW + timedelta(seconds=30),
                failure_count=1,
                last_status=429,
                last_error_code="upstream_http_429",
                updated_at=_NOW,
            )
        )
    async with session.begin():
        gate = await lock_provider_work_gate(
            session, now=_NOW, kind="screening", key=str(uuid4())
        )
        assert gate.admitted is True
        assert gate.probe_required is False


async def test_open_circuit_parks_scoring_without_minting_retry(
    session: AsyncSession,
) -> None:
    agent_id = uuid4()
    epoch = uuid4()
    async with session.begin():
        session.add_all(
            [
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5OutageMiner",
                    name="outage-agent",
                    sha256="ab" * 32,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=_NOW,
                ),
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey="5RunningValidator",
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=_NOW,
                    deadline=_NOW + timedelta(minutes=90),
                    bench_version=7,
                    attempt_count=1,
                    infra_retry_grants=0,
                ),
                ProviderOutageCircuit(
                    provider="openrouter",
                    state="open",
                    epoch=epoch,
                    opened_at=_NOW,
                    retry_at=_NOW + timedelta(minutes=2),
                    last_failure_at=_NOW,
                    failure_count=1,
                    last_status=429,
                    last_error_code="upstream_http_429",
                    updated_at=_NOW,
                ),
            ]
        )

    async with session.begin():
        gate = await lock_provider_work_gate(
            session, now=_NOW, kind="scoring", key="5OtherValidator:slot-0"
        )
        assert gate.circuit is not None
        assert await park_scoring_leases(session, circuit=gate.circuit, now=_NOW) == 1

    async with session.begin():
        ticket = await session.get(ValidatorTicket, (agent_id, 7, "5RunningValidator"))
        assert ticket is not None
        assert ticket.status == TicketStatus.EXPIRED
        assert ticket.attempt_count == 1
        assert ticket.infra_retry_grants == 0
        assert ticket.provider_outage_epoch == epoch
        assert ticket.failure_detail == "provider_outage_parked"

    async with session.begin():
        ticket = await session.get(ValidatorTicket, (agent_id, 7, "5RunningValidator"))
        assert ticket is not None
        ticket.status = TicketStatus.ISSUED
        ticket.deadline = _NOW + timedelta(minutes=90)
        ticket.provider_outage_attempted_epoch = epoch
        circuit = await session.get(ProviderOutageCircuit, "openrouter")
        assert circuit is not None
        circuit.epoch = uuid4()
        gate = await lock_provider_work_gate(
            session, now=_NOW, kind="scoring", key="5OtherValidator:slot-0"
        )
        assert gate.circuit is not None
        assert await park_scoring_leases(session, circuit=gate.circuit, now=_NOW) == 1
        assert ticket.provider_outage_epoch is None
