"""Race and lifecycle tests for pre-payment upload admission."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import UploadAdmissionReservation
from ditto.db.queries.agents import SubmissionCooldownError
from ditto.db.queries.submission_settings import (
    EffectiveSubmissionSettings,
    consume_or_enforce_upload_admission,
    release_upload_admission_for_exact_retry,
    reserve_upload_admission,
)

pytestmark = pytest.mark.asyncio

_PAYMENT_ADDRESS = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


async def test_payment_admission_remains_reusable_for_24_hours(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        await consume_or_enforce_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            admission_token=admission.token,
            settings=settings,
            now=now + timedelta(hours=23, minutes=59),
        )


async def test_unpaid_admission_stops_blocking_after_short_anti_race_window(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        replacement = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            settings=settings,
            now=now + timedelta(minutes=16),
        )

    assert replacement.token != admission.token


async def test_reservation_is_idempotent_and_blocks_competing_series(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=4, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        first = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey-a",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        repeated = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey-a",
            sha256="a" * 64,
            settings=settings,
            now=now + timedelta(seconds=5),
        )
    assert repeated.token == first.token

    with pytest.raises(SubmissionCooldownError):
        async with session.begin():
            await reserve_upload_admission(
                session,
                miner_coldkey="coldkey",
                miner_hotkey="hotkey-b",
                sha256="b" * 64,
                settings=settings,
                now=now + timedelta(seconds=10),
            )


async def test_matching_token_is_consumed_and_legacy_upload_cannot_steal_slot(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey-a",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    with pytest.raises(SubmissionCooldownError):
        async with session.begin():
            await consume_or_enforce_upload_admission(
                session,
                miner_coldkey="coldkey",
                miner_hotkey="hotkey-a",
                sha256="a" * 64,
                admission_token=None,
                settings=settings,
                now=now + timedelta(seconds=1),
            )
    async with session.begin():
        await consume_or_enforce_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey-a",
            sha256="a" * 64,
            admission_token=admission.token,
            settings=settings,
            now=now + timedelta(seconds=2),
        )


async def test_exact_retry_releases_only_its_matching_reservation(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        await release_upload_admission_for_exact_retry(
            session,
            token=admission.token,
            miner_hotkey="different-hotkey",
            sha256="a" * 64,
        )
    assert await session.get(UploadAdmissionReservation, "coldkey") is not None
    await session.rollback()

    async with session.begin():
        await release_upload_admission_for_exact_retry(
            session,
            token=admission.token,
            miner_hotkey="hotkey",
            sha256="a" * 64,
        )
    assert await session.get(UploadAdmissionReservation, "coldkey") is None


async def test_verified_payment_rotates_reservation_to_new_archive(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=4, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        original = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        replacement = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            settings=settings,
            replace_existing=True,
            now=now + timedelta(minutes=5),
        )

    assert replacement.token != original.token
    with pytest.raises(SubmissionCooldownError):
        async with session.begin():
            await consume_or_enforce_upload_admission(
                session,
                miner_coldkey="coldkey",
                miner_hotkey="hotkey",
                sha256="a" * 64,
                admission_token=original.token,
                settings=settings,
                now=now + timedelta(minutes=6),
            )
    async with session.begin():
        await consume_or_enforce_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            admission_token=replacement.token,
            settings=settings,
            now=now + timedelta(minutes=6),
        )


async def test_legacy_payment_reassignment_preserves_original_recovery_deadline(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    expires_at = now + timedelta(hours=24)
    cutoff_at = now + timedelta(minutes=1)
    settings = EffectiveSubmissionSettings(
        revision=4, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        session.add(
            UploadAdmissionReservation(
                miner_coldkey="coldkey",
                token=uuid4(),
                miner_hotkey="hotkey",
                sha256="a" * 64,
                settings_revision=3,
                cooldown_seconds=3600,
                fee_amount_rao=40_000_000,
                legacy_payment_cutoff_at=cutoff_at,
                created_at=now,
                expires_at=expires_at,
            )
        )

    async with session.begin():
        replacement = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            settings=settings,
            replace_existing=True,
            now=now + timedelta(minutes=5),
        )

    assert replacement.expires_at == expires_at
    assert replacement.legacy_payment_cutoff_at == cutoff_at


async def test_verified_payment_cannot_move_reservation_to_different_hotkey(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=4, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey-a",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    with pytest.raises(SubmissionCooldownError):
        async with session.begin():
            await reserve_upload_admission(
                session,
                miner_coldkey="coldkey",
                miner_hotkey="hotkey-b",
                sha256="b" * 64,
                settings=settings,
                replace_existing=True,
                now=now + timedelta(minutes=5),
            )


async def test_in_time_payment_consumes_expired_reservation_at_late_upload(
    session: AsyncSession,
) -> None:
    """Quote at T0, pay at T0+23h, upload at T0+25h: the payment time counts."""
    quoted_at = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=40_000_000,
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=quoted_at,
        )
    async with session.begin():
        await consume_or_enforce_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            admission_token=admission.token,
            settings=settings,
            paid_at=quoted_at + timedelta(hours=23),
            now=quoted_at + timedelta(hours=25),
        )
    assert await session.get(UploadAdmissionReservation, "coldkey") is None


async def test_payment_after_expiry_cannot_consume_the_reservation(
    session: AsyncSession,
) -> None:
    quoted_at = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        admission = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=quoted_at,
        )
    with pytest.raises(SubmissionCooldownError):
        async with session.begin():
            await consume_or_enforce_upload_admission(
                session,
                miner_coldkey="coldkey",
                miner_hotkey="hotkey",
                sha256="a" * 64,
                admission_token=admission.token,
                settings=settings,
                paid_at=quoted_at + timedelta(hours=24, seconds=1),
                now=quoted_at + timedelta(hours=25),
            )


async def test_recovery_of_in_time_payment_keeps_reserved_fee_and_expiry(
    session: AsyncSession,
) -> None:
    """Recovering after expiry must not re-price, nor extend the quote."""
    quoted_at = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    reserved = EffectiveSubmissionSettings(
        revision=1,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=40_000_000,
    )
    changed = EffectiveSubmissionSettings(
        revision=2,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=90_000_000,
    )
    async with session.begin():
        original = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=reserved,
            now=quoted_at,
        )
    async with session.begin():
        recovered = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            settings=changed,
            replace_existing=True,
            paid_at=quoted_at + timedelta(hours=23),
            now=quoted_at + timedelta(hours=25),
        )
    assert recovered.fee_amount_rao == 40_000_000
    assert recovered.expires_at == original.expires_at
    assert recovered.token != original.token
    async with session.begin():
        await consume_or_enforce_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            admission_token=recovered.token,
            settings=changed,
            paid_at=quoted_at + timedelta(hours=23),
            now=quoted_at + timedelta(hours=25, minutes=5),
        )


async def test_recovery_of_late_payment_reprices_at_current_fee(
    session: AsyncSession,
) -> None:
    quoted_at = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    reserved = EffectiveSubmissionSettings(
        revision=1,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=40_000_000,
    )
    changed = EffectiveSubmissionSettings(
        revision=2,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=90_000_000,
    )
    async with session.begin():
        await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=reserved,
            now=quoted_at,
        )
    async with session.begin():
        recovered = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="b" * 64,
            settings=changed,
            replace_existing=True,
            paid_at=quoted_at + timedelta(hours=24, minutes=30),
            now=quoted_at + timedelta(hours=25),
        )
    assert recovered.fee_amount_rao == 90_000_000


async def test_unquotable_settings_never_issue_a_new_reservation(
    session: AsyncSession,
) -> None:
    """An issued reservation is honoured; a new quote is refused (fail closed)."""
    from ditto.api_server.pricing.errors import UnsupportedFeeDenominationError

    now = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    quotable = EffectiveSubmissionSettings(
        revision=1,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=40_000_000,
    )
    unquotable = EffectiveSubmissionSettings(
        revision=2,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=5,
        quotable=False,
    )
    async with session.begin():
        issued = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=quotable,
            now=now,
        )
    async with session.begin():
        again = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=unquotable,
            now=now + timedelta(minutes=5),
        )
    assert again.token == issued.token
    assert again.fee_amount_rao == 40_000_000
    with pytest.raises(UnsupportedFeeDenominationError):
        async with session.begin():
            await reserve_upload_admission(
                session,
                miner_coldkey="other-coldkey",
                miner_hotkey="other-hotkey",
                sha256="b" * 64,
                settings=unquotable,
                now=now,
            )
    assert await session.get(UploadAdmissionReservation, "other-coldkey") is None


@pytest.mark.parametrize("same_archive", [True, False])
async def test_unquotable_recovery_keeps_an_in_time_reservation(
    session: AsyncSession, same_archive: bool
) -> None:
    """kept_for_payment + paid_at must not be blocked by an unquotable revision:
    the reservation keeps its fee and original expiry (rotated or idempotent)."""
    quoted_at = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    reserved = EffectiveSubmissionSettings(
        revision=1,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=40_000_000,
    )
    unquotable = EffectiveSubmissionSettings(
        revision=2,
        cooldown_seconds=3600,
        payment_address=_PAYMENT_ADDRESS,
        fee_amount_rao=5,
        quotable=False,
    )
    async with session.begin():
        original = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=reserved,
            now=quoted_at,
        )
    async with session.begin():
        recovered = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64 if same_archive else "b" * 64,
            settings=unquotable,
            replace_existing=True,
            paid_at=quoted_at + timedelta(hours=23),
            now=quoted_at + timedelta(hours=25),
        )
    assert recovered.fee_amount_rao == 40_000_000
    assert recovered.expires_at == original.expires_at
    assert (recovered.token == original.token) is same_archive


async def test_advertised_and_verified_send_address_share_one_source(
    session: AsyncSession,
) -> None:
    """A legacy reservation without a stored destination advertises the current
    effective deposit address, exactly what verification will require."""
    from ditto.db.queries.submission_settings import reservation_send_address

    now = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    settings = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address=_PAYMENT_ADDRESS
    )
    async with session.begin():
        await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=settings,
            now=now,
        )
    async with session.begin():
        row = await session.get(UploadAdmissionReservation, "coldkey")
        assert row is not None
        row.payment_send_address = None
    rotated = EffectiveSubmissionSettings(
        revision=1, cooldown_seconds=3600, payment_address="5EffectiveAfterBoot"
    )
    async with session.begin():
        advertised = await reserve_upload_admission(
            session,
            miner_coldkey="coldkey",
            miner_hotkey="hotkey",
            sha256="a" * 64,
            settings=rotated,
            now=now + timedelta(minutes=1),
        )
        row = await session.get(UploadAdmissionReservation, "coldkey")
        assert row is not None
        verified = reservation_send_address(
            row, effective_address="5EffectiveAfterBoot"
        )
    assert advertised.payment_send_address == verified == "5EffectiveAfterBoot"
