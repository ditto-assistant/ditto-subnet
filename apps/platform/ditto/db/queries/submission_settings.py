"""Effective miner submission settings and pre-payment reservations."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_settings import (
    MAX_SUBMISSION_FEE_RAO,
    MEMORY_COMPETITION,
    MIN_SUBMISSION_FEE_RAO,
    OVERRIDABLE_COMPETITIONS,
)
from ditto.db.models import SubmissionSettingsRevision, UploadAdmissionReservation
from ditto.db.queries.agents import SubmissionCooldownError, get_submission_retry_at
from ditto.db.queries.submission_deposit_address import (
    effective_submission_deposit_address,
)

DEFAULT_SUBMISSION_COOLDOWN_SECONDS = 3600
DEFAULT_SUBMISSION_FEE_RAO = 200_000_000
MIN_SUBMISSION_COOLDOWN_SECONDS = 60
MAX_SUBMISSION_COOLDOWN_SECONDS = 86400
# A finalized payment may recover its admission for 24 hours. An unpaid
# reservation only excludes a competing archive for 15 minutes; these are
# deliberately separate clocks so a crashed attempt cannot block a coldkey all day.
UPLOAD_ADMISSION_TTL = timedelta(hours=24)
UPLOAD_ADMISSION_BLOCK_TTL = timedelta(minutes=15)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompetitionFeeOverride:
    """One competition's stored override; ``None`` inherits."""

    fee_amount_rao: int | None = None
    fee_required: bool | None = None


@dataclass(frozen=True)
class CompetitionFee:
    """What one competition's upload lane charges right now."""

    competition: str
    fee_amount_rao: int
    amount_source: Literal["default", "override"]
    fee_required: bool
    required_source: Literal["always", "competition_settings", "override"]


def parse_competition_fees(raw: object) -> dict[str, CompetitionFeeOverride]:
    """Read a stored ``competition_fees`` map, failing toward inheritance.

    Only the admin endpoint writes this column, through the validating wire
    model, so a malformed entry means corruption or a newer Platform's
    shape: it is logged and read as "inherit everything" (the default
    price and the competition's own fee switch) rather than refusing every
    upload. An unknown competition id is ignored the same way, so a rollback
    past a newer track keeps working.
    """
    if not isinstance(raw, Mapping):
        if raw not in (None, {}):
            logger.warning("ignoring non-object competition_fees %r", raw)
        return {}
    parsed: dict[str, CompetitionFeeOverride] = {}
    for competition, entry in raw.items():
        if competition not in OVERRIDABLE_COMPETITIONS:
            logger.warning(
                "ignoring fee override for unknown competition %r", competition
            )
            continue
        if not isinstance(entry, Mapping):
            logger.warning("ignoring malformed %s fee override %r", competition, entry)
            continue
        amount = entry.get("fee_amount_rao")
        required = entry.get("fee_required")
        if amount is not None and (
            isinstance(amount, bool)
            or not isinstance(amount, int)
            or not MIN_SUBMISSION_FEE_RAO <= amount <= MAX_SUBMISSION_FEE_RAO
        ):
            logger.warning("ignoring malformed %s fee amount %r", competition, amount)
            amount = None
        if required is not None and not isinstance(required, bool):
            logger.warning(
                "ignoring malformed %s fee_required %r", competition, required
            )
            required = None
        if amount is None and required is None:
            continue
        parsed[competition] = CompetitionFeeOverride(
            fee_amount_rao=amount, fee_required=required
        )
    return parsed


def dump_competition_fees(
    overrides: Mapping[str, CompetitionFeeOverride],
) -> dict[str, dict[str, int | bool | None]]:
    """The canonical stored form: known competitions only, in registry order."""
    return {
        competition: {
            "fee_amount_rao": overrides[competition].fee_amount_rao,
            "fee_required": overrides[competition].fee_required,
        }
        for competition in OVERRIDABLE_COMPETITIONS
        if competition in overrides
    }


@dataclass(frozen=True)
class EffectiveSubmissionSettings:
    revision: int
    cooldown_seconds: int
    payment_address: str
    fee_amount_rao: int = DEFAULT_SUBMISSION_FEE_RAO
    """The default fee: the memory (agent) lane's price, inherited by every
    competition without its own amount."""
    competition_fees: Mapping[str, CompetitionFeeOverride] = field(default_factory=dict)

    def competition_fee(
        self, competition: str, *, competition_requires_fee: bool
    ) -> CompetitionFee:
        """Resolve ``competition``'s price and whether it charges.

        ``competition_requires_fee`` is the competition's own switch (the
        generator ``upload_fee_required``), which decides only while the
        override leaves ``fee_required`` unset. The memory lane always
        charges the default fee.
        """
        if competition == MEMORY_COMPETITION:
            return CompetitionFee(
                competition=competition,
                fee_amount_rao=self.fee_amount_rao,
                amount_source="default",
                fee_required=True,
                required_source="always",
            )
        override = self.competition_fees.get(competition, CompetitionFeeOverride())
        return CompetitionFee(
            competition=competition,
            fee_amount_rao=(
                self.fee_amount_rao
                if override.fee_amount_rao is None
                else override.fee_amount_rao
            ),
            amount_source="default" if override.fee_amount_rao is None else "override",
            fee_required=(
                competition_requires_fee
                if override.fee_required is None
                else override.fee_required
            ),
            required_source=(
                "competition_settings" if override.fee_required is None else "override"
            ),
        )


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


def resolve_submission_settings(
    latest: SubmissionSettingsRevision | None, *, payment_address: str
) -> EffectiveSubmissionSettings:
    """The effective settings of ``latest`` (``None``: the built-in default)."""
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
        competition_fees=parse_competition_fees(latest.competition_fees),
    )


async def effective_submission_settings(
    session: AsyncSession,
    *,
    default_payment_address: str,
) -> EffectiveSubmissionSettings:
    latest = await latest_submission_settings(session)
    payment_address = await effective_submission_deposit_address(
        session, default_address=default_payment_address
    )
    return resolve_submission_settings(latest, payment_address=payment_address)


async def lock_upload_admission_coldkey(
    session: AsyncSession, miner_coldkey: str
) -> None:
    """Serialize one coldkey's admissions until the transaction ends."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:coldkey, 0))"),
            {"coldkey": miner_coldkey},
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _reservation_expiry(row: UploadAdmissionReservation) -> datetime:
    return _utc(row.expires_at)


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
    now: datetime | None = None,
) -> UploadAdmission:
    """Reserve one eligible coldkey slot so payment cannot lose a later race."""
    current = _utc(now or datetime.now(UTC))
    await lock_upload_admission_coldkey(session, miner_coldkey)
    existing = await session.get(
        UploadAdmissionReservation, miner_coldkey, with_for_update=True
    )
    if existing is not None and _reservation_expiry(existing) <= current:
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
                payment_send_address=(
                    existing.payment_send_address or settings.payment_address
                ),
            )
        if replace_existing and existing.miner_hotkey == miner_hotkey:
            # A verified, still-unconsumed payment for this hotkey may fund a
            # different archive. Rotate the opaque token while holding the
            # coldkey lock so an in-flight request for the old bytes cannot win
            # after reassignment.
            existing.token = uuid.uuid4()
            existing.sha256 = sha256
            if existing.legacy_payment_cutoff_at is None:
                existing.created_at = current
                existing.expires_at = current + UPLOAD_ADMISSION_TTL
            await session.flush()
            return UploadAdmission(
                token=existing.token,
                expires_at=_reservation_expiry(existing),
                cooldown_seconds=existing.cooldown_seconds,
                fee_amount_rao=existing.fee_amount_rao,
                legacy_payment_cutoff_at=existing.legacy_payment_cutoff_at,
                payment_send_address=(
                    existing.payment_send_address or settings.payment_address
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
        payment_send_address=row.payment_send_address or settings.payment_address,
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
    now: datetime | None = None,
) -> None:
    """Consume a matching reservation, or enforce cooldown for a legacy client."""
    current = _utc(now or datetime.now(UTC))
    await lock_upload_admission_coldkey(session, miner_coldkey)
    existing = await session.get(
        UploadAdmissionReservation, miner_coldkey, with_for_update=True
    )
    if existing is not None and _reservation_expiry(existing) <= current:
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
