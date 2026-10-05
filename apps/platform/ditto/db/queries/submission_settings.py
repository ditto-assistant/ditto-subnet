"""Effective miner submission settings and pre-payment reservations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ditto.api_models.submission_settings import (
    SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
    SubmissionFeeDenomination,
)
from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError
from ditto.db.models import SubmissionSettingsRevision, UploadAdmissionReservation
from ditto.db.queries.agents import SubmissionCooldownError, get_submission_retry_at
from ditto.db.queries.submission_deposit_address import (
    effective_submission_deposit_address,
)

DEFAULT_SUBMISSION_COOLDOWN_SECONDS = 3600
# Built-in policy in force before any operator revision. It matches the fee the
# migration seeded as revision 1 and the Go upload relay's default
# (``defaultFeeAmountRao``); a parity test pins the two together.
DEFAULT_SUBMISSION_FEE_RAO = 40_000_000
MIN_SUBMISSION_COOLDOWN_SECONDS = 60
MAX_SUBMISSION_COOLDOWN_SECONDS = 86400
# A finalized payment may recover its admission for 24 hours. An unpaid
# reservation only excludes a competing archive for 15 minutes; these are
# deliberately separate clocks so a crashed attempt cannot block a coldkey all day.
UPLOAD_ADMISSION_TTL = timedelta(hours=24)
UPLOAD_ADMISSION_BLOCK_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class EffectiveSubmissionSettings:
    revision: int
    cooldown_seconds: int
    payment_address: str
    fee_amount_rao: int = DEFAULT_SUBMISSION_FEE_RAO
    quotable: bool = True
    """False only when read with ``require_quotable=False`` from a revision in an
    unreviewed denomination; ``fee_amount_rao`` must then never be quoted."""


@dataclass(frozen=True)
class UploadAdmission:
    token: uuid.UUID
    expires_at: datetime
    cooldown_seconds: int
    fee_amount_rao: int
    legacy_payment_cutoff_at: datetime | None
    payment_send_address: str


async def latest_submission_settings(
    session: AsyncSession,
) -> SubmissionSettingsRevision | None:
    return await session.scalar(
        select(SubmissionSettingsRevision)
        .order_by(SubmissionSettingsRevision.revision.desc())
        .limit(1)
    )


def require_supported_fee_denomination(
    row: SubmissionSettingsRevision,
) -> SubmissionFeeDenomination:
    """Refuse to quote from a revision whose denomination this build cannot price.

    ``fixed_tao`` is the only reviewed mode: ``fee_amount_rao`` is the exact
    quote. Any other value (for example a USD target) must never be read as a
    fixed TAO fee, so admission fails closed instead of issuing a wrong quote.
    """
    if row.fee_denomination != SUBMISSION_FEE_DENOMINATION_FIXED_TAO:
        raise UnsupportedFeeDenominationError(
            f"submission settings revision {row.revision} uses fee denomination "
            f"{row.fee_denomination!r}; only "
            f"{SUBMISSION_FEE_DENOMINATION_FIXED_TAO!r} can be quoted"
        )
    return SUBMISSION_FEE_DENOMINATION_FIXED_TAO


async def effective_submission_settings(
    session: AsyncSession,
    *,
    default_payment_address: str,
    require_quotable: bool = True,
) -> EffectiveSubmissionSettings:
    """The latest submission settings.

    Quoting a fee (the default) fails closed on an unreviewed denomination.
    ``require_quotable=False`` is for reads that only need the cooldown or that
    honour an already-issued quote; the result then carries ``quotable`` so a
    caller can never mistake that amount for a fee.
    """
    latest = await latest_submission_settings(session)
    quotable = True
    if latest is not None:
        try:
            require_supported_fee_denomination(latest)
        except UnsupportedFeeDenominationError:
            if require_quotable:
                raise
            quotable = False
    payment_address = await effective_submission_deposit_address(
        session, default_address=default_payment_address
    )
    if latest is None:
        return EffectiveSubmissionSettings(
            revision=0,
            cooldown_seconds=DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
            fee_amount_rao=DEFAULT_SUBMISSION_FEE_RAO,
            payment_address=payment_address,
        )
    return EffectiveSubmissionSettings(
        revision=latest.revision,
        cooldown_seconds=latest.cooldown_seconds,
        fee_amount_rao=latest.fee_amount_rao,
        payment_address=payment_address,
        quotable=quotable,
    )


async def _lock_coldkey(session: AsyncSession, miner_coldkey: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:coldkey, 0))"),
            {"coldkey": miner_coldkey},
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _reservation_expiry(row: UploadAdmissionReservation) -> datetime:
    return _utc(row.expires_at)


def reservation_send_address(
    row: UploadAdmissionReservation, *, effective_address: str
) -> str:
    """The destination a reservation promised, for both quoting and verifying.

    Rows written since the column exists store it. A legacy row with NULL is
    only possible while no deposit-address rotation has happened since it was
    created: every rotation snapshots the then-effective address into NULL
    rows under a table lock. So the address it was quoted under is the current
    effective deposit address. Using one helper keeps the advertised and the
    verified destination identical.
    """
    return row.payment_send_address or effective_address


def _reservation_live(
    row: UploadAdmissionReservation, *, now: datetime, paid_at: datetime | None
) -> bool:
    """Whether a reservation still grants its terms.

    Unexpired now, or -- for a finalized payment -- paid strictly before the
    reservation expired. The payment instant is what fixes the miner's
    obligation, so a late upload of an in-time payment keeps its quote.
    """
    expiry = _reservation_expiry(row)
    if expiry > now:
        return True
    return paid_at is not None and _utc(paid_at) < expiry


def _reservation_block_until(row: UploadAdmissionReservation) -> datetime:
    return min(
        _reservation_expiry(row),
        _utc(row.created_at) + UPLOAD_ADMISSION_BLOCK_TTL,
    )


async def reserve_upload_admission(
    session: AsyncSession,
    *,
    miner_coldkey: str,
    miner_hotkey: str,
    sha256: str,
    settings: EffectiveSubmissionSettings,
    replace_existing: bool = False,
    paid_at: datetime | None = None,
    now: datetime | None = None,
) -> UploadAdmission:
    """Reserve one eligible coldkey slot so payment cannot lose a later race.

    ``paid_at`` is the block time of a verified, unconsumed payment being
    recovered (``replace_existing``). A reservation that has since expired but
    was still live when that payment finalized is kept, with its fee and its
    original expiry, so recovery never re-prices an in-time payment.
    """
    current = _utc(now or datetime.now(UTC))
    await _lock_coldkey(session, miner_coldkey)
    existing = await session.get(
        UploadAdmissionReservation, miner_coldkey, with_for_update=True
    )
    kept_for_payment = (
        existing is not None
        and replace_existing
        and existing.miner_hotkey == miner_hotkey
        and _reservation_expiry(existing) <= current
        and _reservation_live(existing, now=current, paid_at=paid_at)
    )
    if (
        existing is not None
        and not kept_for_payment
        and _reservation_expiry(existing) <= current
    ):
        await session.delete(existing)
        await session.flush()
        existing = None
    if existing is not None:
        if existing.miner_hotkey == miner_hotkey and existing.sha256 == sha256:
            return UploadAdmission(
                token=existing.token,
                expires_at=_reservation_expiry(existing),
                cooldown_seconds=existing.cooldown_seconds,
                fee_amount_rao=existing.fee_amount_rao,
                legacy_payment_cutoff_at=existing.legacy_payment_cutoff_at,
                payment_send_address=reservation_send_address(
                    existing, effective_address=settings.payment_address
                ),
            )
        if replace_existing and existing.miner_hotkey == miner_hotkey:
            # A verified, still-unconsumed payment for this hotkey may fund a
            # different archive. Rotate the opaque token while holding the
            # coldkey lock so an in-flight request for the old bytes cannot win
            # after reassignment.
            existing.token = uuid.uuid4()
            existing.sha256 = sha256
            # Rotation never moves created_at or expires_at, live or kept: the
            # recovered payment is honoured by its own block time, and an
            # extension would let a fresh payment made after the original
            # expiry claim the old fee (repeatably, by recovering again).
            await session.flush()
            return UploadAdmission(
                token=existing.token,
                expires_at=_reservation_expiry(existing),
                cooldown_seconds=existing.cooldown_seconds,
                fee_amount_rao=existing.fee_amount_rao,
                legacy_payment_cutoff_at=existing.legacy_payment_cutoff_at,
                payment_send_address=reservation_send_address(
                    existing, effective_address=settings.payment_address
                ),
            )
        block_until = _reservation_block_until(existing)
        if block_until > current:
            raise SubmissionCooldownError(block_until)
        await session.delete(existing)
        await session.flush()
        existing = None

    if not replace_existing:
        retry_at = await get_submission_retry_at(
            session,
            miner_coldkey=miner_coldkey,
            cooldown=timedelta(seconds=settings.cooldown_seconds),
            now=current,
        )
        if retry_at is not None:
            raise SubmissionCooldownError(retry_at)

    if not settings.quotable:
        # A new quote is never issued from a revision this build cannot price;
        # only an already-issued reservation (handled above) is honoured.
        raise UnsupportedFeeDenominationError(
            f"submission settings revision {settings.revision} cannot be quoted"
        )
    row = UploadAdmissionReservation(
        miner_coldkey=miner_coldkey,
        token=uuid.uuid4(),
        miner_hotkey=miner_hotkey,
        sha256=sha256,
        settings_revision=settings.revision,
        cooldown_seconds=settings.cooldown_seconds,
        fee_amount_rao=settings.fee_amount_rao,
        payment_send_address=settings.payment_address,
        legacy_payment_cutoff_at=None,
        created_at=current,
        expires_at=current + UPLOAD_ADMISSION_TTL,
    )
    session.add(row)
    await session.flush()
    return UploadAdmission(
        token=row.token,
        expires_at=row.expires_at,
        cooldown_seconds=row.cooldown_seconds,
        fee_amount_rao=row.fee_amount_rao,
        legacy_payment_cutoff_at=row.legacy_payment_cutoff_at,
        payment_send_address=reservation_send_address(
            row, effective_address=settings.payment_address
        ),
    )


async def get_upload_admission(
    session: AsyncSession, *, token: uuid.UUID
) -> UploadAdmissionReservation | None:
    return await session.scalar(
        select(UploadAdmissionReservation).where(
            UploadAdmissionReservation.token == token
        )
    )


async def get_upload_admission_for_coldkey(
    session: AsyncSession, *, miner_coldkey: str
) -> UploadAdmissionReservation | None:
    return await session.get(UploadAdmissionReservation, miner_coldkey)


async def release_upload_admission_for_exact_retry(
    session: AsyncSession,
    *,
    token: uuid.UUID,
    miner_hotkey: str,
    sha256: str,
) -> None:
    """Remove the temporary reservation created to recover an exact retry."""
    await session.execute(
        delete(UploadAdmissionReservation).where(
            UploadAdmissionReservation.token == token,
            UploadAdmissionReservation.miner_hotkey == miner_hotkey,
            UploadAdmissionReservation.sha256 == sha256,
        )
    )


async def consume_or_enforce_upload_admission(
    session: AsyncSession,
    *,
    miner_coldkey: str,
    miner_hotkey: str,
    sha256: str,
    admission_token: uuid.UUID | None,
    settings: EffectiveSubmissionSettings,
    paid_at: datetime | None = None,
    now: datetime | None = None,
) -> None:
    """Consume a matching reservation, or enforce cooldown for a legacy client.

    ``paid_at`` is the block time of the payment funding this upload. The
    reservation named by ``admission_token`` is honoured if that payment
    finalized before the reservation expired, even when the upload arrives
    later (within the payment's own recovery window).
    """
    current = _utc(now or datetime.now(UTC))
    await _lock_coldkey(session, miner_coldkey)
    existing = await session.get(
        UploadAdmissionReservation, miner_coldkey, with_for_update=True
    )
    token_matches = (
        existing is not None
        and admission_token is not None
        and existing.token == admission_token
        and existing.miner_hotkey == miner_hotkey
        and existing.sha256 == sha256
    )
    if (
        existing is not None
        and not (
            token_matches and _reservation_live(existing, now=current, paid_at=paid_at)
        )
        and _reservation_expiry(existing) <= current
    ):
        await session.delete(existing)
        await session.flush()
        existing = None

    if admission_token is not None:
        if (
            existing is None
            or existing.token != admission_token
            or existing.miner_hotkey != miner_hotkey
            or existing.sha256 != sha256
        ):
            retry_at = (
                _reservation_block_until(existing)
                if existing is not None
                else current + timedelta(seconds=60)
            )
            raise SubmissionCooldownError(
                retry_at if retry_at > current else current + timedelta(seconds=60)
            )
        await session.delete(existing)
        await session.flush()
        return

    if existing is not None:
        block_until = _reservation_block_until(existing)
        if block_until > current:
            raise SubmissionCooldownError(block_until)
        await session.delete(existing)
        await session.flush()

    submission_retry_at = await get_submission_retry_at(
        session,
        miner_coldkey=miner_coldkey,
        cooldown=timedelta(seconds=settings.cooldown_seconds),
        now=current,
    )
    if submission_retry_at is not None:
        raise SubmissionCooldownError(submission_retry_at)


async def submission_settings_history(
    session: AsyncSession, *, limit: int
) -> list[tuple[SubmissionSettingsRevision, SubmissionSettingsRevision | None]]:
    """Newest-first revisions, each paired with the parent it replaced.

    The parent is the revision the operator previewed and confirmed against
    (``parent_revision``), which is not necessarily ``revision - 1``: a failed
    insert still consumes a sequence value. The first revision's parent (0)
    is returned as the current build's built-in default policy (an unsaved
    revision 0). That is a reference point, not a record of what was charged
    before revision 1: production's revision 1 predates the fixed-TAO
    setting, so its "previous" fee is this build's default, never actually
    charged.
    """
    parent = aliased(SubmissionSettingsRevision)
    rows = (
        await session.execute(
            select(SubmissionSettingsRevision, parent)
            .outerjoin(
                parent,
                parent.revision == SubmissionSettingsRevision.parent_revision,
            )
            .order_by(SubmissionSettingsRevision.revision.desc())
            .limit(limit)
        )
    ).all()
    return [
        (
            row,
            previous
            if previous is not None or row.parent_revision != 0
            else built_in_submission_settings(),
        )
        for row, previous in rows
    ]


def built_in_submission_settings() -> SubmissionSettingsRevision:
    """The policy in force before any operator revision (unsaved, revision 0)."""
    return SubmissionSettingsRevision(
        revision=0,
        parent_revision=0,
        cooldown_seconds=DEFAULT_SUBMISSION_COOLDOWN_SECONDS,
        fee_amount_rao=DEFAULT_SUBMISSION_FEE_RAO,
        fee_denomination=SUBMISSION_FEE_DENOMINATION_FIXED_TAO,
        reason="Built-in submission settings",
        actor="platform",
        created_at=None,
    )


@dataclass(frozen=True)
class InFlightQuotes:
    count: int
    at_other_fees: int
    expire_by: datetime | None
    recoverable_expired: int = 0
    recoverable_expired_at_other_fees: int = 0
    recoverable_until: datetime | None = None


async def in_flight_quotes(
    session: AsyncSession,
    *,
    proposed_fee_amount_rao: int,
    now: datetime | None = None,
) -> InFlightQuotes:
    """Summarize reservations whose issued fee can still bind a payment.

    Unexpired reservations bind any payment made before they expire. An
    expired one still binds a payment that finalized before its expiry, and
    such a payment is recoverable for ``UPLOAD_ADMISSION_TTL`` after its block
    time, so a reservation that expired less than that long ago may still be
    honoured. That second group is an upper bound: which of them actually
    back an in-time payment is only known when the payment is presented.
    """
    current = _utc(now or datetime.now(UTC))
    other_fee = UploadAdmissionReservation.fee_amount_rao != proposed_fee_amount_rao
    live = UploadAdmissionReservation.expires_at > current
    recoverable = (UploadAdmissionReservation.expires_at <= current) & (
        UploadAdmissionReservation.expires_at > current - UPLOAD_ADMISSION_TTL
    )
    (
        count,
        at_other_fees,
        expire_by,
        recoverable_count,
        recoverable_other,
        recoverable_last_expiry,
    ) = (
        await session.execute(
            select(
                func.count().filter(live),
                func.count().filter(live & other_fee),
                func.max(UploadAdmissionReservation.expires_at).filter(live),
                func.count().filter(recoverable),
                func.count().filter(recoverable & other_fee),
                func.max(UploadAdmissionReservation.expires_at).filter(recoverable),
            ).where(
                UploadAdmissionReservation.expires_at > current - UPLOAD_ADMISSION_TTL
            )
        )
    ).one()
    return InFlightQuotes(
        count=int(count),
        at_other_fees=int(at_other_fees),
        expire_by=_utc(expire_by) if expire_by is not None else None,
        recoverable_expired=int(recoverable_count),
        recoverable_expired_at_other_fees=int(recoverable_other),
        # A payment made just before the last expiry is recoverable until then
        # plus the recovery window.
        recoverable_until=(
            _utc(recoverable_last_expiry) + UPLOAD_ADMISSION_TTL
            if recoverable_last_expiry is not None
            else None
        ),
    )
