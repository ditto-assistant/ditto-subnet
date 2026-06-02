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
from ditto.db.models import EvaluationPayment
from ditto.db.queries.agents import insert_agent
from ditto.db.queries.payments import (
    _PAYMENT_REPLAY_CONSTRAINT,
    insert_evaluation_payment,
)


def _make_verified(**overrides: Any) -> VerifiedPayment:
    base: dict[str, Any] = {
        "block_hash": "0xblock",
        "extrinsic_index": 3,
        "miner_hotkey": "5HKAlphaHotkey",
        "miner_coldkey": "5CKAlphaColdkey",
        "amount_rao": 5_000_000_000,
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
                ip_address=None,
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
        assert row.dest_address == verified.dest_address
        assert row.miner_coldkey == verified.miner_coldkey


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
