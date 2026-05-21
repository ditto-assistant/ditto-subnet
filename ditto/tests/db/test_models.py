"""Unit tests for ditto.db.models.

Scope is the ``from_row`` adapters and ``AgentStatus`` enum coverage.
Frozen-dataclass behaviour and field-default declarations are language
guarantees and not retested here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from ditto.db.models import Agent, AgentStatus, EvaluationPayment


def make_agent_row(**overrides: Any) -> dict[str, Any]:
    """Build an asyncpg-row-shaped dict for the ``agents`` table."""
    base: dict[str, Any] = {
        "agent_id": uuid4(),
        "miner_hotkey": "5HKsomething",
        "name": "alpha",
        "sha256": "deadbeef" * 8,
        "status": "uploaded",
        "ip_address": "192.0.2.1",
        "created_at": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def make_payment_row(**overrides: Any) -> dict[str, Any]:
    """Build an asyncpg-row-shaped dict for the ``evaluation_payments`` table."""
    base: dict[str, Any] = {
        "block_hash": "0xblock",
        "extrinsic_index": 3,
        "agent_id": uuid4(),
        "miner_hotkey": "5HKsomething",
        "miner_coldkey": "5CKsomething",
        "amount_rao": 5_000_000_000,
        "dest_address": "5Dest",
        "timestamp": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 19, 12, 0, 5, tzinfo=UTC),
    }
    base.update(overrides)
    return base


class TestAgentStatusEnum:
    """AgentStatus must expose every status value used by the migration."""

    def test_every_postgres_enum_value_present(self):
        """The set of Python enum values must equal the ``CREATE TYPE`` body."""
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


class TestAgentFromRow:
    """Tests for :meth:`Agent.from_row`."""

    def test_happy_path_maps_every_column(self):
        agent_id = uuid4()
        created = datetime(2026, 5, 19, tzinfo=UTC)
        row = make_agent_row(
            agent_id=agent_id,
            miner_hotkey="5HK1",
            name="bravo",
            sha256="abc",
            status="evaluating",
            ip_address="10.0.0.1",
            created_at=created,
        )

        agent = Agent.from_row(row)

        assert agent.agent_id == agent_id
        assert agent.miner_hotkey == "5HK1"
        assert agent.name == "bravo"
        assert agent.sha256 == "abc"
        assert agent.status is AgentStatus.EVALUATING
        assert agent.ip_address == "10.0.0.1"
        assert agent.created_at == created

    def test_ip_address_can_be_none(self):
        """``agents.ip_address`` is nullable in the schema."""
        row = make_agent_row(ip_address=None)
        agent = Agent.from_row(row)
        assert agent.ip_address is None

    def test_unknown_status_value_raises_value_error(self):
        """``AgentStatus`` is the boundary between Postgres ENUM and Python."""
        row = make_agent_row(status="bogus")
        with pytest.raises(ValueError):
            Agent.from_row(row)


class TestEvaluationPaymentFromRow:
    """Tests for :meth:`EvaluationPayment.from_row`."""

    def test_happy_path_maps_every_column(self):
        agent_id = uuid4()
        ts = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
        created = datetime(2026, 5, 19, 12, 0, 5, tzinfo=UTC)
        row = make_payment_row(
            block_hash="0xabc",
            extrinsic_index=7,
            agent_id=agent_id,
            miner_hotkey="5HK1",
            miner_coldkey="5CK1",
            amount_rao=1_234_567_890,
            dest_address="5Dest",
            timestamp=ts,
            created_at=created,
        )

        payment = EvaluationPayment.from_row(row)

        assert payment.block_hash == "0xabc"
        assert payment.extrinsic_index == 7
        assert payment.agent_id == agent_id
        assert payment.miner_hotkey == "5HK1"
        assert payment.miner_coldkey == "5CK1"
        assert payment.amount_rao == 1_234_567_890
        assert payment.dest_address == "5Dest"
        assert payment.timestamp == ts
        assert payment.created_at == created
