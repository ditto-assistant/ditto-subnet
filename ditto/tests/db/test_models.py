"""Unit tests for ditto.db.models.

Tests exercise the declarative ORM against a real SQLite engine so
constraint enforcement, type round-tripping, and ORM hydration are
verified end-to-end rather than mocked. Postgres-specific behaviour
(native ENUM type, partial indexes) is covered by Layer 3 integration
tests in a future PR.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import Agent, AgentStatus, EvaluationPayment


def make_agent(**overrides: Any) -> Agent:
    """Build an :class:`Agent` with sensible defaults for tests."""
    base: dict[str, Any] = {
        "agent_id": uuid4(),
        "miner_hotkey": "5HKsomething",
        "name": "alpha",
        "sha256": "deadbeef" * 8,
        "status": AgentStatus.UPLOADED,
        "ip_address": "192.0.2.1",
        "created_at": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Agent(**base)


def make_payment(agent: Agent, **overrides: Any) -> EvaluationPayment:
    """Build an :class:`EvaluationPayment` referencing ``agent`` by default."""
    base: dict[str, Any] = {
        "block_hash": "0xblock",
        "extrinsic_index": 3,
        "agent_id": agent.agent_id,
        "miner_hotkey": agent.miner_hotkey,
        "miner_coldkey": "5CKsomething",
        "amount_rao": 5_000_000_000,
        "dest_address": "5Dest",
        "timestamp": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 19, 12, 0, 5, tzinfo=UTC),
    }
    base.update(overrides)
    return EvaluationPayment(**base)


class TestAgentStatusEnum:
    """AgentStatus must expose every status value used by the migration."""

    def test_every_postgres_enum_value_present(self):
        expected = {
            "uploaded",
            "screening",
            "screening_passed",
            "screening_failed",
            "evaluating",
            "scored",
            "live",
            "ath_pending_review",
            "banned",
        }
        assert {s.value for s in AgentStatus} == expected


class TestAgentRoundTrip:
    """Insert + select cycles via a real :class:`AsyncSession`."""

    async def test_happy_path_round_trip(self, session: AsyncSession):
        original = make_agent(status=AgentStatus.EVALUATING)
        session.add(original)
        await session.commit()

        result = await session.scalar(
            select(Agent).where(Agent.agent_id == original.agent_id)
        )

        assert result is not None
        assert result.agent_id == original.agent_id
        assert result.miner_hotkey == original.miner_hotkey
        assert result.name == original.name
        assert result.sha256 == original.sha256
        assert result.status is AgentStatus.EVALUATING
        assert result.ip_address == original.ip_address
        assert result.created_at == original.created_at

    async def test_ip_address_nullable(self, session: AsyncSession):
        original = make_agent(ip_address=None)
        session.add(original)
        await session.commit()

        result = await session.scalar(
            select(Agent).where(Agent.agent_id == original.agent_id)
        )

        assert result is not None
        assert result.ip_address is None


class TestEvaluationPaymentRoundTrip:
    """Insert + select cycles via a real :class:`AsyncSession`."""

    async def test_happy_path_round_trip(self, session: AsyncSession):
        agent = make_agent()
        session.add(agent)
        await session.commit()

        payment = make_payment(agent)
        session.add(payment)
        await session.commit()

        result = await session.scalar(
            select(EvaluationPayment).where(
                EvaluationPayment.agent_id == agent.agent_id
            )
        )

        assert result is not None
        assert result.block_hash == payment.block_hash
        assert result.extrinsic_index == payment.extrinsic_index
        assert result.agent_id == agent.agent_id
        assert result.miner_hotkey == agent.miner_hotkey
        assert result.miner_coldkey == payment.miner_coldkey
        assert result.amount_rao == payment.amount_rao
        assert result.dest_address == payment.dest_address


class TestEvaluationPaymentConstraints:
    """Schema-level invariants documented in DDL must actually fire."""

    async def test_composite_pk_blocks_replay(self, session: AsyncSession):
        """Same (block_hash, extrinsic_index) pair cannot be inserted twice."""
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent))
        await session.commit()

        second_agent = make_agent(agent_id=uuid4(), miner_hotkey=agent.miner_hotkey)
        session.add(second_agent)
        await session.commit()

        session.add(
            make_payment(
                second_agent,
                block_hash=make_payment(agent).block_hash,
                extrinsic_index=make_payment(agent).extrinsic_index,
            )
        )
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_unique_agent_id_enforces_one_payment_per_agent(
        self, session: AsyncSession
    ):
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent))
        await session.commit()

        session.add(
            make_payment(
                agent,
                block_hash="0xother",
                extrinsic_index=99,
            )
        )
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_composite_fk_rejects_mismatched_hotkey(self, session: AsyncSession):
        """(agent_id, miner_hotkey) must point at an existing agent row."""
        agent = make_agent(miner_hotkey="5HK1")
        session.add(agent)
        await session.commit()

        session.add(
            make_payment(
                agent,
                miner_hotkey="5HK_OTHER",
            )
        )
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_amount_rao_check_rejects_zero(self, session: AsyncSession):
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent, amount_rao=0))
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_amount_rao_check_rejects_negative(self, session: AsyncSession):
        """``CHECK (amount_rao > 0)`` covers negative values too."""
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent, amount_rao=-1))
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_extrinsic_index_check_rejects_negative(self, session: AsyncSession):
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent, extrinsic_index=-1))
        with pytest.raises(SAIntegrityError):
            await session.commit()

    async def test_on_delete_restrict_blocks_agent_deletion(
        self, session: AsyncSession
    ):
        """``ON DELETE RESTRICT`` on the composite FK keeps payments un-orphaned."""
        agent = make_agent()
        session.add(agent)
        await session.commit()

        session.add(make_payment(agent))
        await session.commit()

        await session.delete(agent)
        with pytest.raises(SAIntegrityError):
            await session.commit()


class TestAgentConstraints:
    """Schema-level invariants on the agents table."""

    async def test_agent_id_primary_key_blocks_duplicate(
        self, session_maker: async_sessionmaker[AsyncSession]
    ):
        """Second insert in a fresh session hits the DB PK, not SA's identity map."""
        first = make_agent()
        async with session_maker() as session1:
            session1.add(first)
            await session1.commit()

        async with session_maker() as session2:
            duplicate = make_agent(
                agent_id=first.agent_id,
                miner_hotkey="5HK_DIFFERENT",
            )
            session2.add(duplicate)
            with pytest.raises(SAIntegrityError):
                await session2.commit()


class TestAgentStatusBoundary:
    """The ``agentstatus`` ENUM column rejects values outside the type."""

    async def test_python_enum_rejects_unknown_value(self):
        """``AgentStatus`` parsing fails before the row reaches SQLAlchemy."""
        with pytest.raises(ValueError):
            AgentStatus("bogus")

    async def test_db_rejects_unknown_status_value(self, session: AsyncSession):
        """Raw INSERT with an unknown status value fires the DB-level guard."""
        agent_id = uuid4()
        with pytest.raises((SAIntegrityError, StatementError)):
            await session.execute(
                text(
                    "INSERT INTO agents "
                    "(agent_id, miner_hotkey, name, sha256, status, created_at) "
                    "VALUES (:agent_id, :hotkey, :name, :sha, :status, :ts)"
                ),
                {
                    "agent_id": str(agent_id),
                    "hotkey": "5HK1",
                    "name": "alpha",
                    "sha": "abc",
                    "status": "bogus",
                    "ts": datetime(2026, 5, 19, tzinfo=UTC),
                },
            )
            await session.commit()


class TestAgentIdType:
    """``agent_id`` round-trips as a :class:`UUID` instance, not a string."""

    async def test_agent_id_is_uuid_after_load(self, session: AsyncSession):
        original = make_agent()
        session.add(original)
        await session.commit()

        result = await session.scalar(
            select(Agent).where(Agent.agent_id == original.agent_id)
        )
        assert result is not None
        assert isinstance(result.agent_id, UUID)
