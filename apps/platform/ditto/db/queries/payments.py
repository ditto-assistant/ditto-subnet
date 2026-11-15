"""Mutations against the upload-payment tables.

Three lanes record the finalized transfers miners pay:

* ``evaluation_payments``: proofs that fund agent uploads, plus unspent
  credits (a byte-identical upload, or a readmission refused after payment);
* ``generator_submission_payments``: proofs that fund generator uploads;
* ``readmission_payments``: proofs that paid a readmission of an existing
  agent into one benchmark era (SN-485).

A proof funds exactly one thing across all three. Every insert takes the
shared per-proof advisory lock (:func:`lock_payment_proof`), refuses a proof
any other lane already recorded, then inserts; each table's primary key stops
a replay within its own lane. The one deliberate overlap is a credited
readmission: its ``readmission_payments`` row (``outcome = 'credited'``) is
the accounting record and its ``evaluation_payments`` credit row is what a
later upload or readmission spends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from asyncpg.exceptions import UniqueViolationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError

# PaymentReplayedError is a payment-domain outcome that happens to be
# detected at persistence time. Importing the typed error from the
# payment_verifier module keeps the entire 32xx error family in one
# place even though the raise site lives in ditto.db. Same direction
# the shipped PaymentVerifier already uses by importing chain.errors.
from ditto.api_server.payment_verifier import PaymentReplayedError
from ditto.db.errors import IntegrityError as DbIntegrityError
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    GeneratorSubmissionPayment,
    ReadmissionPayment,
)

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
_GENERATOR_PAYMENT_REPLAY_CONSTRAINT = "generator_submission_payments_pkey"
_READMISSION_PAYMENT_REPLAY_CONSTRAINT = "readmission_payments_pkey"
_READMISSION_ADMITTED_ONCE_CONSTRAINT = "readmission_payments_admitted_once"

CREDIT_ORIGIN_READMISSION = "readmission"
UPLOAD_CREDIT_TTL = timedelta(hours=24)
"""How long a byte-identical-upload credit stays spendable (the upload
recovery window)."""
READMISSION_CREDIT_TTL = timedelta(days=30)
"""How long a credit banked by a readmission refused after payment stays
spendable (SN-485 O5: at least 30 days, not 24 hours)."""

ReadmissionOutcome = Literal["admitted", "credited"]


class ReadmissionAlreadyAdmittedError(Exception):
    """This agent already has an admitted readmission in this era."""


def credit_ttl(payment: EvaluationPayment) -> timedelta:
    """How long ``payment`` stays spendable as a credit, by its origin."""
    if payment.credit_origin == CREDIT_ORIGIN_READMISSION:
        return READMISSION_CREDIT_TTL
    return UPLOAD_CREDIT_TTL


def credit_expires_at(payment: EvaluationPayment) -> datetime:
    """When the credit ``payment`` stops being spendable."""
    paid_at = payment.timestamp
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=UTC)
    return paid_at + credit_ttl(payment)


def credit_expired(payment: EvaluationPayment, *, now: datetime | None = None) -> bool:
    return credit_expires_at(payment) <= (now or datetime.now(UTC))


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


async def lock_payment_proof(
    session: AsyncSession, *, block_hash: str, extrinsic_index: int
) -> None:
    """Serialize every upload that spends one proof until the transaction ends.

    Agent uploads record a proof in ``evaluation_payments`` and generator
    uploads in ``generator_submission_payments``; each table's primary key
    stops a replay within its lane. Both lanes take this lock before checking
    the other lane's table and inserting their own row, so two concurrent
    uploads (one per lane) cannot both spend the same transfer.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"upload-payment:{block_hash.lower()}:{extrinsic_index}"},
    )


async def get_generator_payment_for_proof(
    session: AsyncSession, *, block_hash: str, extrinsic_index: int
) -> GeneratorSubmissionPayment | None:
    """Return the generator upload a proof already funded, if any."""
    return await session.scalar(
        select(GeneratorSubmissionPayment).where(
            GeneratorSubmissionPayment.block_hash == block_hash.lower(),
            GeneratorSubmissionPayment.extrinsic_index == extrinsic_index,
        )
    )


async def get_readmission_payment_for_proof(
    session: AsyncSession,
    *,
    block_hash: str,
    extrinsic_index: int,
    for_update: bool = False,
) -> ReadmissionPayment | None:
    """Return the readmission a proof paid for (or was credited from), if any."""
    stmt = select(ReadmissionPayment).where(
        ReadmissionPayment.block_hash == block_hash.lower(),
        ReadmissionPayment.extrinsic_index == extrinsic_index,
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

    Refuses a proof the generator or readmission lane already recorded.

    Exactly one destination is required: ``agent_id`` assigns the proof to a
    new evaluation, while ``credit_for_agent_id`` records why an identical paid
    upload became a reusable credit. The caller wraps assignment together with
    :func:`insert_agent` in one ``async with session.begin():`` block so both
    rows commit atomically. A PK violation rolls the agent insert back.

    Raises:
        PaymentReplayedError: Composite-PK collision on
            ``(block_hash, extrinsic_index)``, or the proof already funded
            a generator upload. The envelope handler maps
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
    # The cross-lane check must not flush the caller's pending rows: their
    # constraint violations keep surfacing from the flush below.
    with session.no_autoflush:
        await lock_payment_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        funded_generator = await get_generator_payment_for_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        funded_readmission = await get_readmission_payment_for_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
    if funded_generator:
        raise PaymentReplayedError(
            f"payment proof (block_hash={verified.block_hash}, "
            f"extrinsic_index={verified.extrinsic_index}) already funded a "
            "generator upload"
        )
    if funded_readmission:
        raise PaymentReplayedError(
            f"payment proof (block_hash={verified.block_hash}, "
            f"extrinsic_index={verified.extrinsic_index}) already paid a "
            "readmission"
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


async def insert_generator_payment(
    session: AsyncSession, *, verified: VerifiedPayment, submission_id: UUID
) -> None:
    """Bind a verified proof to the generator submission it funded.

    Runs inside the caller's upload transaction. Takes the shared proof lock,
    refuses a proof already recorded by either lane, then inserts; a primary
    key collision (a concurrent generator upload that committed first) is the
    same replay.

    Raises:
        PaymentReplayedError: the proof already funded an agent or generator
            upload or a readmission, or is held as an agent upload credit.
    """
    with session.no_autoflush:
        await lock_payment_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        agent_lane = await get_evaluation_payment_for_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        generator_lane = await get_generator_payment_for_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        readmission_lane = await get_readmission_payment_for_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
    if (
        agent_lane is not None
        or generator_lane is not None
        or readmission_lane is not None
    ):
        raise PaymentReplayedError(
            f"payment proof (block_hash={verified.block_hash}, "
            f"extrinsic_index={verified.extrinsic_index}) already used"
        )
    session.add(
        GeneratorSubmissionPayment(
            block_hash=verified.block_hash.lower(),
            extrinsic_index=verified.extrinsic_index,
            submission_id=submission_id,
            miner_hotkey=verified.miner_hotkey,
            miner_coldkey=verified.miner_coldkey,
            amount_rao=verified.amount_rao,
            tao_usd_rate=verified.tao_usd_rate,
            dest_address=verified.dest_address,
            timestamp=verified.block_timestamp,
        )
    )
    try:
        await session.flush()
    except SAIntegrityError as e:
        asyncpg_err = e.orig.__cause__ if e.orig is not None else None
        if (
            isinstance(asyncpg_err, UniqueViolationError)
            and (getattr(asyncpg_err, "constraint_name", "") or "")
            == _GENERATOR_PAYMENT_REPLAY_CONSTRAINT
        ):
            raise PaymentReplayedError("payment proof already used") from e
        raise DbIntegrityError(
            f"generator_submission_payments insert violated constraint: {e.orig}"
        ) from e


def _unique_violation_name(error: SAIntegrityError) -> str | None:
    asyncpg_err = error.orig.__cause__ if error.orig is not None else None
    if not isinstance(asyncpg_err, UniqueViolationError):
        return None
    return getattr(asyncpg_err, "constraint_name", "") or ""


async def insert_readmission_payment(
    session: AsyncSession,
    *,
    verified: VerifiedPayment,
    agent_id: UUID,
    bench_version: int,
    rollout_id: UUID | None,
    outcome: ReadmissionOutcome = "admitted",
    refusal_reason: str | None = None,
) -> ReadmissionPayment:
    """Record a fresh proof in the readmission lane, in the caller's transaction.

    ``admitted`` binds the proof to the readmission written in the same
    transaction. ``credited`` records a readmission refused after payment and
    banks the proof as an ``evaluation_payments`` credit for ``agent_id`` with
    ``credit_origin = 'readmission'`` (spendable for 30 days by the same hotkey,
    on an upload or a readmission).

    Raises:
        PaymentReplayedError: the proof is already recorded by any lane.
        ReadmissionAlreadyAdmittedError: ``agent_id`` already has an admitted
            readmission for ``bench_version`` (``admitted`` only).
    """
    if (outcome == "credited") != bool(refusal_reason and refusal_reason.strip()):
        raise ValueError("a credited readmission needs a refusal reason; only it")
    with session.no_autoflush:
        await lock_payment_proof(
            session,
            block_hash=verified.block_hash,
            extrinsic_index=verified.extrinsic_index,
        )
        lanes = (
            await get_evaluation_payment_for_proof(
                session,
                block_hash=verified.block_hash,
                extrinsic_index=verified.extrinsic_index,
            ),
            await get_generator_payment_for_proof(
                session,
                block_hash=verified.block_hash,
                extrinsic_index=verified.extrinsic_index,
            ),
            await get_readmission_payment_for_proof(
                session,
                block_hash=verified.block_hash,
                extrinsic_index=verified.extrinsic_index,
            ),
        )
    if any(lane is not None for lane in lanes):
        raise PaymentReplayedError(
            f"payment proof (block_hash={verified.block_hash}, "
            f"extrinsic_index={verified.extrinsic_index}) already used"
        )
    row = ReadmissionPayment(
        block_hash=verified.block_hash.lower(),
        extrinsic_index=verified.extrinsic_index,
        agent_id=agent_id,
        bench_version=bench_version,
        rollout_id=rollout_id,
        miner_hotkey=verified.miner_hotkey,
        miner_coldkey=verified.miner_coldkey,
        amount_rao=verified.amount_rao,
        tao_usd_rate=verified.tao_usd_rate,
        dest_address=verified.dest_address,
        timestamp=verified.block_timestamp,
        funding="fresh",
        outcome=outcome,
        refusal_reason=refusal_reason.strip() if refusal_reason else None,
    )
    session.add(row)
    if outcome == "credited":
        session.add(
            EvaluationPayment(
                block_hash=verified.block_hash.lower(),
                extrinsic_index=verified.extrinsic_index,
                credit_for_agent_id=agent_id,
                credit_origin=CREDIT_ORIGIN_READMISSION,
                miner_hotkey=verified.miner_hotkey,
                miner_coldkey=verified.miner_coldkey,
                amount_rao=verified.amount_rao,
                tao_usd_rate=verified.tao_usd_rate,
                dest_address=verified.dest_address,
                timestamp=verified.block_timestamp,
            )
        )
    try:
        await session.flush()
    except SAIntegrityError as e:
        cname = _unique_violation_name(e)
        if cname in (
            _READMISSION_PAYMENT_REPLAY_CONSTRAINT,
            _PAYMENT_REPLAY_CONSTRAINT,
        ):
            raise PaymentReplayedError("payment proof already used") from e
        if cname == _READMISSION_ADMITTED_ONCE_CONSTRAINT:
            raise ReadmissionAlreadyAdmittedError(
                f"agent {agent_id} already has an admitted v{bench_version} readmission"
            ) from e
        raise DbIntegrityError(
            f"readmission_payments insert violated constraint: {e.orig}"
        ) from e
    return row


async def spend_credit_on_readmission(
    session: AsyncSession,
    *,
    credit: EvaluationPayment,
    agent_id: UUID,
    bench_version: int,
    rollout_id: UUID | None,
    miner_hotkey: str,
    now: datetime | None = None,
) -> ReadmissionPayment:
    """Pay a readmission with an unspent upload credit, in the caller's transaction.

    ``credit`` must be locked (``for_update``), unspent, the same hotkey's and
    still inside its window. The proof moves to the readmission lane: a credit
    banked by an earlier refused readmission flips that ``credited`` row to
    ``admitted`` for this target, any other credit gets a new ``admitted`` row,
    and the ``evaluation_payments`` credit row is removed so no upload can spend
    the proof again. ``funding = 'credit'`` and ``credit_audit`` keep that
    history: the credit's origin and, for a readmission-origin credit, the
    earlier refusal.

    Raises:
        PaymentReplayedError: the credit is spent, another hotkey's, or expired.
        ReadmissionAlreadyAdmittedError: as :func:`insert_readmission_payment`.
    """
    current = now or datetime.now(UTC)
    if credit.agent_id is not None or credit.credit_for_agent_id is None:
        raise PaymentReplayedError("payment credit is no longer available")
    if credit.miner_hotkey != miner_hotkey:
        raise PaymentReplayedError("payment credit belongs to a different hotkey")
    if credit_expired(credit, now=current):
        raise PaymentReplayedError("payment credit has expired")
    await lock_payment_proof(
        session, block_hash=credit.block_hash, extrinsic_index=credit.extrinsic_index
    )
    existing = await get_readmission_payment_for_proof(
        session,
        block_hash=credit.block_hash,
        extrinsic_index=credit.extrinsic_index,
        for_update=True,
    )
    if existing is not None and existing.outcome != "credited":
        raise PaymentReplayedError("payment proof already paid a readmission")
    # Spending removes the evaluation_payments credit row below, so keep its
    # history (and an earlier refusal's) on the readmission row.
    credit_audit: dict[str, object] = {
        "credit_origin": credit.credit_origin or "duplicate_upload",
        "credit_for_agent_id": str(credit.credit_for_agent_id),
        "credit_recorded_at": (
            credit.created_at.isoformat() if credit.created_at else None
        ),
    }
    if existing is not None:
        credit_audit.update(
            {
                "refusal_reason": existing.refusal_reason,
                "credited_agent_id": str(existing.agent_id),
                "credited_bench_version": existing.bench_version,
                "credited_at": existing.created_at.isoformat()
                if existing.created_at
                else None,
            }
        )
    if existing is None:
        row = ReadmissionPayment(
            block_hash=credit.block_hash.lower(),
            extrinsic_index=credit.extrinsic_index,
            miner_hotkey=credit.miner_hotkey,
            miner_coldkey=credit.miner_coldkey,
            amount_rao=credit.amount_rao,
            tao_usd_rate=credit.tao_usd_rate,
            dest_address=credit.dest_address,
            timestamp=credit.timestamp,
            agent_id=agent_id,
            bench_version=bench_version,
            rollout_id=rollout_id,
            funding="credit",
            outcome="admitted",
            refusal_reason=None,
            credit_audit=credit_audit,
        )
        session.add(row)
    else:
        row = existing
        row.agent_id = agent_id
        row.bench_version = bench_version
        row.rollout_id = rollout_id
        row.funding = "credit"
        row.outcome = "admitted"
        row.refusal_reason = None
        row.credit_audit = credit_audit
        row.updated_at = current
    await session.execute(
        delete(EvaluationPayment).where(
            EvaluationPayment.block_hash == credit.block_hash,
            EvaluationPayment.extrinsic_index == credit.extrinsic_index,
            EvaluationPayment.agent_id.is_(None),
        )
    )
    try:
        await session.flush()
    except SAIntegrityError as e:
        if _unique_violation_name(e) == _READMISSION_ADMITTED_ONCE_CONSTRAINT:
            raise ReadmissionAlreadyAdmittedError(
                f"agent {agent_id} already has an admitted v{bench_version} readmission"
            ) from e
        raise DbIntegrityError(
            f"readmission_payments credit spend violated constraint: {e.orig}"
        ) from e
    return row
