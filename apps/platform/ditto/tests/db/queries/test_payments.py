"""Unit tests for :mod:`ditto.db.queries.payments`.

The happy path runs against SQLite-in-memory so the ORM mapping is
exercised real. The replay-dispatch branches need an asyncpg-specific
``UniqueViolationError`` wrapped in :class:`SAIntegrityError`; SQLite
cannot reproduce that shape, so those branches use a mocked session
that raises the synthetic exception directly. Both layers together
cover the dispatch + the actual row write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from asyncpg.exceptions import (
    ForeignKeyViolationError,
    IntegrityConstraintViolationError,
    UniqueViolationError,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.payment_verifier import PaymentReplayedError, VerifiedPayment
from ditto.db import IntegrityError as DbIntegrityError
from ditto.db.models import Agent, EvaluationPayment
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import (
    _PAYMENT_REPLAY_CONSTRAINT,
    consume_evaluation_credit,
    get_agent_for_payment_proof,
    get_evaluation_payment_for_proof,
    insert_evaluation_payment,
)


def _make_verified(**overrides: Any) -> VerifiedPayment:
    base: dict[str, Any] = {
        "block_hash": "0xblock",
        "extrinsic_index": 3,
        "miner_hotkey": "5HKAlphaHotkey",
        "miner_coldkey": "5CKAlphaColdkey",
        "amount_rao": 5_000_000_000,
        "tao_usd_rate": Decimal("400"),
        "dest_address": "5DestAddress",
        "block_timestamp": datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return VerifiedPayment(**base)


def _make_unique_violation(
    *, constraint_name: str | None = _PAYMENT_REPLAY_CONSTRAINT
) -> UniqueViolationError:
    err = UniqueViolationError("duplicate key value")
    # asyncpg populates ``constraint_name`` from the wire protocol; set
    # it directly here so the dispatch sees the expected shape.
    err.constraint_name = constraint_name  # type: ignore[assignment]
    return err


def _wrap_in_sa(asyncpg_err: Exception) -> SAIntegrityError:
    """Mimic SA's asyncpg dialect: ``e.orig`` is SA's own wrapper, the
    asyncpg exception lives on ``e.orig.__cause__``. The dispatch under
    test reads ``e.orig.__cause__`` so the synthetic must match.
    """

    class _SAWrappedIntegrity(Exception):
        pass

    wrapper = _SAWrappedIntegrity("wrapped")
    wrapper.__cause__ = asyncpg_err
    return SAIntegrityError(statement="...", params=(), orig=wrapper)


def _mock_session(flush_side_effect: BaseException | None = None) -> MagicMock:
    """A MagicMock standing in for AsyncSession.

    Configured so ``session.add(...)`` accepts anything (the ORM row),
    ``await session.flush()`` raises whatever the test passed in.
    """
    session = MagicMock(spec=AsyncSession)
    session.add = MagicMock(return_value=None)
    if flush_side_effect is not None:
        session.flush = AsyncMock(side_effect=flush_side_effect)
    else:
        session.flush = AsyncMock(return_value=None)
    return session


class TestInsertEvaluationPaymentHappyPath:
    async def test_block_hash_is_canonicalized_for_insert_and_lookup(
        self, session: AsyncSession
    ) -> None:
        agent_id = uuid4()
        verified = _make_verified(block_hash="0xABCDEF")

        async with session.begin():
            await insert_agent(
                session,
                agent_id=agent_id,
                miner_hotkey=verified.miner_hotkey,
                name="canonical-hash",
                sha256="ca" * 32,
                size_bytes=10,
            )
            await insert_evaluation_payment(
                session, verified=verified, agent_id=agent_id
            )

        payment = await get_evaluation_payment_for_proof(
            session,
            block_hash="0xAbCdEf",
            extrinsic_index=verified.extrinsic_index,
        )
        assert payment is not None
        assert payment.block_hash == "0xabcdef"

    async def test_lookup_finds_legacy_mixed_case_block_hash(
        self, session: AsyncSession
    ) -> None:
        agent_id = uuid4()
        verified = _make_verified(block_hash="0xabcdef")

        async with session.begin():
            await insert_agent(
                session,
                agent_id=agent_id,
                miner_hotkey=verified.miner_hotkey,
                name="legacy-mixed-case-hash",
                sha256="cb" * 32,
                size_bytes=10,
            )
            await insert_evaluation_payment(
                session, verified=verified, agent_id=agent_id
            )
            payment = await get_evaluation_payment_for_proof(
                session,
                block_hash=verified.block_hash,
                extrinsic_index=verified.extrinsic_index,
            )
            assert payment is not None
            payment.block_hash = "0xAbCdEf"

        payment = await get_evaluation_payment_for_proof(
            session,
            block_hash="0xabcdef",
            extrinsic_index=verified.extrinsic_index,
        )
        agent = await get_agent_for_payment_proof(
            session,
            block_hash="0xABCDEF",
            extrinsic_index=verified.extrinsic_index,
        )

        assert payment is not None
        assert payment.block_hash == "0xAbCdEf"
        assert agent is not None
        assert agent.agent_id == agent_id

    async def test_inserts_row(self, session: AsyncSession):
        agent_id = uuid4()
        verified = _make_verified()

        async with session.begin():
            await insert_agent(
                session,
                agent_id=agent_id,
                miner_hotkey=verified.miner_hotkey,
                name="alpha-agent",
                sha256="deadbeef" * 8,
                size_bytes=524288,
            )
            await insert_evaluation_payment(
                session, verified=verified, agent_id=agent_id
            )

        row = (
            await session.execute(
                select(EvaluationPayment).where(
                    EvaluationPayment.block_hash == verified.block_hash
                )
            )
        ).scalar_one()
        assert row.agent_id == agent_id
        assert row.amount_rao == verified.amount_rao
        assert row.tao_usd_rate == verified.tao_usd_rate
        assert row.dest_address == verified.dest_address
        assert row.miner_coldkey == verified.miner_coldkey

        funded_agent = await get_agent_for_payment_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        assert isinstance(funded_agent, Agent)
        assert funded_agent.agent_id == agent_id

    async def test_lookup_returns_none_for_unused_proof(self, session: AsyncSession):
        assert (
            await get_agent_for_payment_proof(
                session,
                block_hash="0xunused",
                extrinsic_index=99,
            )
            is None
        )

    async def test_identical_payment_becomes_reusable_credit(
        self, session: AsyncSession
    ) -> None:
        source_id = uuid4()
        target_id = uuid4()
        verified = _make_verified()
        credit = _make_verified(block_hash="0xcredit", extrinsic_index=4)
        async with session.begin():
            await insert_agent(
                session,
                agent_id=source_id,
                miner_hotkey=verified.miner_hotkey,
                name="source",
                sha256="aa" * 32,
                size_bytes=10,
            )
            await insert_evaluation_payment(
                session, verified=verified, agent_id=source_id
            )
            await insert_evaluation_payment(
                session, verified=credit, credit_for_agent_id=source_id
            )

        assert (
            await get_agent_for_payment_proof(
                session,
                block_hash=credit.block_hash,
                extrinsic_index=credit.extrinsic_index,
            )
            is None
        )
        await session.rollback()

        async with session.begin():
            await insert_agent(
                session,
                agent_id=target_id,
                miner_hotkey=credit.miner_hotkey,
                name="target",
                sha256="bb" * 32,
                size_bytes=11,
            )
            payment = await get_evaluation_payment_for_proof(
                session,
                block_hash=credit.block_hash,
                extrinsic_index=credit.extrinsic_index,
                for_update=True,
            )
            assert payment is not None
            await consume_evaluation_credit(
                session,
                payment=payment,
                agent_id=target_id,
                miner_hotkey=credit.miner_hotkey,
            )

        consumed = await get_evaluation_payment_for_proof(
            session,
            block_hash=credit.block_hash,
            extrinsic_index=credit.extrinsic_index,
        )
        assert consumed is not None
        assert consumed.agent_id == target_id
        assert consumed.credit_for_agent_id is None


class TestInsertEvaluationPaymentReplayDispatch:
    async def test_pk_collision_raises_payment_replayed(self):
        session = _mock_session(flush_side_effect=_wrap_in_sa(_make_unique_violation()))
        verified = _make_verified()

        with pytest.raises(PaymentReplayedError, match="block_hash=0xblock"):
            await insert_evaluation_payment(
                session, verified=verified, agent_id=uuid4()
            )

    async def test_pk_replay_chains_original_cause(self):
        session = _mock_session(flush_side_effect=_wrap_in_sa(_make_unique_violation()))

        with pytest.raises(PaymentReplayedError) as info:
            await insert_evaluation_payment(
                session, verified=_make_verified(), agent_id=uuid4()
            )
        assert info.value.__cause__ is not None
        assert isinstance(info.value.__cause__, SAIntegrityError)


class TestInsertEvaluationPaymentOtherConstraints:
    async def test_unique_violation_with_different_constraint_name_falls_through(self):
        """UNIQUE(agent_id) collisions are programmer-bug territory, not
        miner replay. Must surface as the generic DbIntegrityError."""
        session = _mock_session(
            flush_side_effect=_wrap_in_sa(
                _make_unique_violation(
                    constraint_name="evaluation_payments_agent_id_key"
                )
            )
        )

        with pytest.raises(DbIntegrityError):
            await insert_evaluation_payment(
                session, verified=_make_verified(), agent_id=uuid4()
            )

    async def test_unique_violation_with_none_constraint_name_falls_through(self):
        """``constraint_name`` can be ``None`` on edge driver paths; the
        ``getattr(..., "") or ""`` guard treats it as 'not the replay
        constraint' so we re-raise as a generic integrity error rather
        than crash on ``None == "..."``."""
        session = _mock_session(
            flush_side_effect=_wrap_in_sa(_make_unique_violation(constraint_name=None))
        )

        with pytest.raises(DbIntegrityError):
            await insert_evaluation_payment(
                session, verified=_make_verified(), agent_id=uuid4()
            )

    async def test_foreign_key_violation_falls_through(self):
        """FK violation indicates the agent insert never landed; that is a
        programmer bug in this codebase, not a miner-facing event."""
        fk_err = ForeignKeyViolationError("fk violation")
        session = _mock_session(flush_side_effect=_wrap_in_sa(fk_err))

        with pytest.raises(DbIntegrityError):
            await insert_evaluation_payment(
                session, verified=_make_verified(), agent_id=uuid4()
            )

    async def test_other_integrity_subclass_falls_through(self):
        """Non-UniqueViolation IntegrityConstraintViolation siblings (CHECK
        constraints, NOT NULL) must not get classified as replay."""
        check_err = IntegrityConstraintViolationError("check violation")
        session = _mock_session(flush_side_effect=_wrap_in_sa(check_err))

        with pytest.raises(DbIntegrityError):
            await insert_evaluation_payment(
                session, verified=_make_verified(), agent_id=uuid4()
            )


class TestKeywordOnlyContract:
    async def test_positional_args_rejected(self):
        """All non-session args must be keyword-only so callers can't
        swap the VerifiedPayment + agent_id by accident."""
        session = _mock_session()
        with pytest.raises(TypeError):
            await insert_evaluation_payment(  # type: ignore[misc]
                session, _make_verified(), uuid4()
            )
