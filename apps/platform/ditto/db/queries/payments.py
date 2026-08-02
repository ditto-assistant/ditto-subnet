"""Mutations against the ``evaluation_payments`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from asyncpg.exceptions import UniqueViolationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError

# PaymentReplayedError is a payment-domain outcome that happens to be
# detected at persistence time. Importing the typed error from the
# payment_verifier module keeps the entire 32xx error family in one
# place even though the raise site lives in ditto.db. Same direction
# the shipped PaymentVerifier already uses by importing chain.errors.
from ditto.api_server.payment_verifier import PaymentReplayedError
from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import Agent, EvaluationPayment

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


async def get_miner_coldkey_for_agent(
    session: AsyncSession, *, agent_id: UUID
) -> str | None:
    """Return the immutable payment-time coldkey for an agent.

    ``None`` is possible only for legacy/test agents created before paid-upload
    provenance was mandatory.
    """
    return await session.scalar(
        select(EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.agent_id == agent_id
        )
    )


async def get_miner_coldkeys_for_agents(
    session: AsyncSession, *, agent_ids: set[UUID]
) -> dict[UUID, str]:
    """Batch payment-time ownership lookup for operator comparison pages."""
    if not agent_ids:
        return {}
    rows = await session.execute(
        select(EvaluationPayment.agent_id, EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.agent_id.in_(agent_ids)
        )
    )
    return {
        agent_id: miner_coldkey
        for agent_id, miner_coldkey in rows.tuples().all()
        if agent_id is not None
    }


async def get_agent_for_payment_proof(
    session: AsyncSession,
    *,
    block_hash: str,
    extrinsic_index: int,
) -> Agent | None:
    """Return the agent already funded by a payment proof, if any.

    ``POST /upload/agent`` uses this lookup to make an exact retry idempotent.
    A gateway failure can hide a successful response after the database commit;
    returning the original row prevents the miner from paying a second fee.
    Callers must still authenticate the hotkey and compare the immutable upload
    identity (hotkey, name, and tar SHA) before treating the request as a retry.
    """
    return await session.scalar(
        select(Agent)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            func.lower(EvaluationPayment.block_hash) == block_hash.lower(),
            EvaluationPayment.extrinsic_index == extrinsic_index,
        )
    )


async def get_evaluation_payment_for_proof(
    session: AsyncSession,
    *,
    block_hash: str,
    extrinsic_index: int,
    for_update: bool = False,
) -> EvaluationPayment | None:
    """Return the replay-protection row for a proof, including open credits."""
    stmt = select(EvaluationPayment).where(
        func.lower(EvaluationPayment.block_hash) == block_hash.lower(),
        EvaluationPayment.extrinsic_index == extrinsic_index,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def get_same_owner_agent_by_sha(
    session: AsyncSession, *, miner_coldkey: str, sha256: str
) -> Agent | None:
    """Return the earliest paid submission with identical bytes for one owner."""
    return await session.scalar(
        select(Agent)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            EvaluationPayment.miner_coldkey == miner_coldkey,
            Agent.sha256 == sha256,
        )
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(1)
    )


async def get_same_hotkey_agent_by_sha(
    session: AsyncSession, *, miner_hotkey: str, sha256: str
) -> Agent | None:
    """Cheap pre-payment duplicate check before coldkey proof is available."""
    result = await session.execute(
        select(Agent)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(Agent.miner_hotkey == miner_hotkey, Agent.sha256 == sha256)
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none() if result is not None else None


async def insert_evaluation_payment(
    session: AsyncSession,
    *,
    verified: VerifiedPayment,
    agent_id: UUID | None = None,
    credit_for_agent_id: UUID | None = None,
) -> None:
    """Insert one ``evaluation_payments`` row inside the caller's transaction.

    Exactly one destination is required: ``agent_id`` assigns the proof to a
    new evaluation, while ``credit_for_agent_id`` records why an identical paid
    upload became a reusable credit. The caller wraps assignment together with
    :func:`insert_agent` in one ``async with session.begin():`` block so both
    rows commit atomically. A PK violation rolls the agent insert back.

    Raises:
        PaymentReplayedError: Composite-PK collision on
            ``(block_hash, extrinsic_index)``. The envelope handler maps
            this to HTTP 402 + error code 3207. Closes threat-model row
            P1 (replay same payment proof twice).
        DbIntegrityError: Any other constraint violation
            (UNIQUE ``(agent_id)``, an agent FK, or a CHECK constraint).
            These all indicate a programmer
            bug rather than a miner action; the envelope catch-all
            maps to HTTP 500.
    """
    if (agent_id is None) == (credit_for_agent_id is None):
        raise ValueError(
            "exactly one of agent_id or credit_for_agent_id must be supplied"
        )
    row = EvaluationPayment(
        block_hash=verified.block_hash.lower(),
        extrinsic_index=verified.extrinsic_index,
        agent_id=agent_id,
        credit_for_agent_id=credit_for_agent_id,
        miner_hotkey=verified.miner_hotkey,
        miner_coldkey=verified.miner_coldkey,
        amount_rao=verified.amount_rao,
        accepted_under_legacy_fee_amnesty=(verified.accepted_under_legacy_fee_amnesty),
        tao_usd_rate=verified.tao_usd_rate,
        dest_address=verified.dest_address,
        timestamp=verified.block_timestamp,
    )
    session.add(row)
    try:
        await session.flush()
    except SAIntegrityError as e:
        # SA's asyncpg dialect wraps the underlying asyncpg exception
        # one layer deep: ``e.orig`` is SA's own dbapi-compat
        # IntegrityError, and the real asyncpg exception (carrying
        # ``constraint_name``) sits on ``e.orig.__cause__``. Walk the
        # cause to recover the asyncpg shape.
        asyncpg_err = e.orig.__cause__ if e.orig is not None else None
        if isinstance(asyncpg_err, UniqueViolationError):
            # ``constraint_name`` can be empty on edge paths (driver
            # version differences, certain replication setups); the
            # ``or ""`` guard keeps the comparison total instead of
            # crashing on ``None``.
            cname = getattr(asyncpg_err, "constraint_name", "") or ""
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


async def consume_evaluation_credit(
    session: AsyncSession,
    *,
    payment: EvaluationPayment,
    agent_id: UUID,
    miner_hotkey: str,
) -> None:
    """Atomically bind a locked available payment credit to a new agent."""
    if payment.agent_id is not None or payment.credit_for_agent_id is None:
        raise PaymentReplayedError("payment credit is no longer available")
    if payment.miner_hotkey != miner_hotkey:
        raise PaymentReplayedError("payment credit belongs to a different hotkey")
    payment.agent_id = agent_id
    payment.credit_for_agent_id = None
    await session.flush()
