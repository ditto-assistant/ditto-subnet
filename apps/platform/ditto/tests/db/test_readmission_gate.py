"""The SN-485 readmission activation predicate (pure)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ditto.db.queries.readmission_gate import (
    READMISSION_MIN_BENCH_VERSION,
    READMISSION_NOTICE_PERIOD,
    ReadmissionAnnouncementError,
    readmission_activation,
    submitted_before_activation,
    validate_new_announcement,
)

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def _activation(announced_at: datetime | None, bench_version: int = 19):
    return readmission_activation(
        announced_at=announced_at,
        active_bench_version=bench_version,
        fee_amount_rao=75_000_000,
        settings_revision=4,
        now=NOW,
    )


def test_unset_announcement_is_inactive_whatever_the_bench() -> None:
    state = _activation(None, bench_version=40)
    assert not state.active
    assert state.inactive_reasons == ("announcement_unset",)
    assert state.announced_at is None and state.effective_at is None
    # Nothing is ever charged while unset, however old the submission.
    assert submitted_before_activation(state, NOW + timedelta(days=365))


def test_v18_is_never_charged_even_after_the_notice() -> None:
    state = _activation(NOW - timedelta(days=30), bench_version=18)
    assert not state.active
    assert state.inactive_reasons == ("bench_version_below_minimum",)
    assert READMISSION_MIN_BENCH_VERSION == 19


def test_notice_period_holds_the_fee_for_seven_days() -> None:
    announced = NOW - READMISSION_NOTICE_PERIOD + timedelta(seconds=1)
    state = _activation(announced)
    assert not state.active
    assert state.inactive_reasons == ("notice_period",)
    assert state.effective_at == announced + timedelta(days=7)


def test_every_unmet_condition_is_reported() -> None:
    state = _activation(NOW, bench_version=18)
    assert state.inactive_reasons == ("bench_version_below_minimum", "notice_period")


def test_active_from_exactly_seven_days_on_v19() -> None:
    announced = NOW - READMISSION_NOTICE_PERIOD
    state = _activation(announced)
    assert state.active
    assert state.inactive_reasons == ()
    assert state.effective_at == NOW
    assert state.fee_amount_rao == 75_000_000
    assert state.notice_period_seconds == 7 * 86400
    # A submission made before the fee went live keeps today's behaviour.
    assert submitted_before_activation(state, NOW - timedelta(seconds=1))
    assert not submitted_before_activation(state, NOW)


def test_naive_announcement_reads_as_utc() -> None:
    state = _activation(datetime(2026, 11, 1))
    assert state.announced_at == datetime(2026, 11, 1, tzinfo=UTC)


def test_new_announcement_is_normalized_to_utc_seconds() -> None:
    offset = timezone(timedelta(hours=2))
    value = datetime(2026, 12, 1, 14, 0, 0, 123456, tzinfo=offset)
    assert validate_new_announcement(value, now=NOW) == datetime(
        2026, 12, 1, 12, 0, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (datetime(2026, 12, 1, 12, 0), "timezone"),
        (NOW - timedelta(hours=1, seconds=1), "backdated"),
        (NOW + timedelta(days=30, seconds=1), "ahead"),
    ],
)
def test_new_announcement_outside_the_window_is_refused(
    value: datetime, message: str
) -> None:
    with pytest.raises(ReadmissionAnnouncementError, match=message):
        validate_new_announcement(value, now=NOW)
