"""Mutations against the ``evaluation_payments`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asyncpg.exceptions import UniqueViolationError
from sqlalchemy.exc import IntegrityError as SAIntegrityError

# PaymentReplayedError is a payment-domain outcome that happens to be
# detected at persistence time. Importing the typed error from the
# payment_verifier module keeps the entire 32xx error family in one
# place even though the raise site lives in ditto.db. Same direction
# the shipped PaymentVerifier already uses by importing chain.errors.
from ditto.api_server.payment_verifier import PaymentReplayedError
from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import EvaluationPayment

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.payment_verifier import VerifiedPayment

# Sourced from the initial-schema migration. Postgres assigns the PK
# constraint its default name when CREATE TABLE uses an inline
# ``PRIMARY KEY`` clause without ``CONSTRAINT <name>``; that default
# matches the explicit name in :mod:`ditto.db.models`. The layer-3
# integration test asserts this still holds at runtime so any future
# migration that renames the PK is caught before the dispatch silently
# stops translating replays into PaymentReplayedError.
_PAYMENT_REPLAY_CONSTRAINT = "evaluation_payments_pkey"


async def insert_evaluation_payment(
    session: AsyncSession,
    *,
    verified: VerifiedPayment,
    agent_id: UUID,
) -> None:
    """Insert one ``evaluation_payments`` row inside the caller's transaction.

    Caller wraps this together with :func:`insert_agent` in one
    ``async with session.begin():`` block so both rows commit atomically.
    A PK violation on the payment insert rolls the agent insert back.

    Raises:
        PaymentReplayedError: Composite-PK collision on
            ``(block_hash, extrinsic_index)``. The envelope handler maps
            this to HTTP 402 + error code 3207. Closes threat-model row
            P1 (replay same payment proof twice).
        DbIntegrityError: Any other constraint violation
            (UNIQUE ``(agent_id)``, the composite FK to ``agents``, or
            either CHECK constraint). These all indicate a programmer
            bug rather than a miner action; the envelope catch-all
            maps to HTTP 500.
    """
    row = EvaluationPayment(
        block_hash=verified.block_hash,
        extrinsic_index=verified.extrinsic_index,
        agent_id=agent_id,
        miner_hotkey=verified.miner_hotkey,
        miner_coldkey=verified.miner_coldkey,
        amount_rao=verified.amount_rao,
        dest_address=verified.dest_address,
        timestamp=verified.block_timestamp,
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as e:
        if isinstance(e.orig, UniqueViolationError):
            # ``constraint_name`` can be empty on edge paths (driver
            # version differences, certain replication setups); the
            # ``or ""`` guard keeps the comparison total instead of
            # crashing on ``None``.
            cname = getattr(e.orig, "constraint_name", "") or ""
            if cname == _PAYMENT_REPLAY_CONSTRAINT:
                raise PaymentReplayedError(
                    f"payment proof "
                    f"(block_hash={verified.block_hash}, "
                    f"extrinsic_index={verified.extrinsic_index}) "
                    f"already used"
                ) from e
        raise DbIntegrityError(
            f"evaluation_payments insert violated constraint: {e.orig}"
        ) from e
