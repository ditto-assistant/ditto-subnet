from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from ditto.api_server.confirmation_bundles import (
    BudgetExhausted,
    BudgetReservation,
    ConfirmationBundleKey,
    ConfirmationBundleSettings,
    ConfirmationMode,
    ConfirmationPolicyError,
    ConfirmationProfileRef,
    DailyBudgetSnapshot,
    ReservationState,
    StaleBudgetSnapshot,
    reserve_daily_budget,
    settle_daily_budget,
    utc_day,
)

_DAY = date(2026, 8, 8)
_KEY = ConfirmationBundleKey(
    artifact_sha256="b" * 64,
    bench_version=9,
    profile_revision="launch-v1",
    profile_checksum="a" * 64,
)


def settings(
    *,
    mode: ConfirmationMode = ConfirmationMode.ENFORCE,
    bundle_cap: int = 5,
    dollar_cap: int = 1_000,
) -> ConfirmationBundleSettings:
    return ConfirmationBundleSettings(
        mode=mode,
        top_n=5,
        daily_bundle_cap=bundle_cap,
        daily_dollar_cap_microusd=dollar_cap,
        per_bundle_request_cap=100,
        per_bundle_token_cap=1_000_000,
        profile=ConfirmationProfileRef(revision="launch-v1", checksum="a" * 64),
    )


def reserve(
    snapshot: DailyBudgetSnapshot,
    *,
    amount: int = 100,
    reservation_id: str = "reservation-1",
    policy: ConfirmationBundleSettings | None = None,
) -> tuple[DailyBudgetSnapshot, BudgetReservation]:
    return reserve_daily_budget(
        snapshot,
        expected_revision=snapshot.revision,
        settings=policy or settings(),
        bundle_key=_KEY,
        reservation_id=reservation_id,
        reserve_microusd=amount,
    )


class TestUtcDay:
    def test_utc_timestamp_uses_same_day(self) -> None:
        now = datetime(2026, 8, 8, 23, 59, 59, tzinfo=UTC)
        assert utc_day(now) == date(2026, 8, 8)

    def test_positive_offset_is_converted_to_utc(self) -> None:
        now = datetime(2026, 8, 9, 1, 30, tzinfo=timezone(timedelta(hours=2)))
        assert utc_day(now) == date(2026, 8, 8)

    def test_negative_offset_is_converted_to_utc(self) -> None:
        now = datetime(2026, 8, 8, 21, 30, tzinfo=timezone(timedelta(hours=-4)))
        assert utc_day(now) == date(2026, 8, 9)

    def test_exact_utc_midnight_rolls_day(self) -> None:
        before = datetime(2026, 8, 8, 23, 59, 59, 999999, tzinfo=UTC)
        after = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)
        assert utc_day(before) == date(2026, 8, 8)
        assert utc_day(after) == date(2026, 8, 9)

    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="timezone-aware"):
            utc_day(datetime(2026, 8, 8, 12))

    def test_snapshot_factory_uses_utc_conversion(self) -> None:
        now = datetime(2026, 8, 8, 21, 30, tzinfo=timezone(timedelta(hours=-4)))
        snapshot = DailyBudgetSnapshot.for_time(now)
        assert snapshot.utc_day == date(2026, 8, 9)
        assert snapshot.revision == 0
        assert snapshot.issued_attempts == 0

    def test_rollover_creates_independent_fresh_snapshot(self) -> None:
        late = DailyBudgetSnapshot.for_time(
            datetime(2026, 8, 8, 23, 59, 59, tzinfo=UTC)
        )
        late, reservation = reserve(late, amount=1_000)
        assert late.committed_microusd == 1_000
        next_day = DailyBudgetSnapshot.for_time(
            datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)
        )
        assert next_day.utc_day != reservation.utc_day
        assert next_day.committed_microusd == 0
        next_day, _ = reserve(next_day, amount=1_000, reservation_id="next")
        assert next_day.committed_microusd == 1_000


class TestSnapshotValidation:
    def test_default_snapshot_is_zeroed(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        assert snapshot.revision == 0
        assert snapshot.issued_attempts == 0
        assert snapshot.outstanding_reserved_microusd == 0
        assert snapshot.settled_microusd == 0
        assert snapshot.committed_microusd == 0

    def test_committed_is_settled_plus_outstanding(self) -> None:
        snapshot = DailyBudgetSnapshot(
            utc_day=_DAY,
            outstanding_reserved_microusd=400,
            settled_microusd=600,
        )
        assert snapshot.committed_microusd == 1_000

    @pytest.mark.parametrize(
        "field",
        [
            "revision",
            "issued_attempts",
            "outstanding_reserved_microusd",
            "settled_microusd",
        ],
    )
    @pytest.mark.parametrize("value", [-1, True, 1.0, (1 << 62) + 1])
    def test_rejects_invalid_counter(self, field: str, value: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match=field):
            DailyBudgetSnapshot(utc_day=_DAY, **{field: value})  # type: ignore[arg-type]

    def test_snapshot_is_frozen(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        with pytest.raises(FrozenInstanceError):
            snapshot.revision = 1  # type: ignore[misc]


class TestReservationValidation:
    def test_valid_reserved_record(self) -> None:
        reservation = BudgetReservation(
            reservation_id="r",
            bundle_key=_KEY,
            utc_day=_DAY,
            reserved_microusd=100,
        )
        assert reservation.state == ReservationState.RESERVED
        assert reservation.actual_microusd is None

    def test_valid_settled_record(self) -> None:
        reservation = BudgetReservation(
            reservation_id="r",
            bundle_key=_KEY,
            utc_day=_DAY,
            reserved_microusd=100,
            state=ReservationState.SETTLED,
            actual_microusd=75,
            failed_attempt=False,
        )
        assert reservation.actual_microusd == 75

    def test_requires_reservation_id(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="id is required"):
            BudgetReservation(
                reservation_id="",
                bundle_key=_KEY,
                utc_day=_DAY,
                reserved_microusd=100,
            )

    @pytest.mark.parametrize("amount", [-1, 0, True, 1.0, (1 << 62) + 1])
    def test_rejects_invalid_reserved_amount(self, amount: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="reserved_microusd"):
            BudgetReservation(
                reservation_id="r",
                bundle_key=_KEY,
                utc_day=_DAY,
                reserved_microusd=amount,  # type: ignore[arg-type]
            )

    def test_unsettled_cannot_claim_actual_cost(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="unsettled"):
            BudgetReservation(
                reservation_id="r",
                bundle_key=_KEY,
                utc_day=_DAY,
                reserved_microusd=100,
                actual_microusd=75,
            )

    def test_unsettled_cannot_claim_failure_outcome(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="unsettled"):
            BudgetReservation(
                reservation_id="r",
                bundle_key=_KEY,
                utc_day=_DAY,
                reserved_microusd=100,
                failed_attempt=False,
            )

    @pytest.mark.parametrize(
        "actual,failed",
        [(None, False), (0, None), (None, None)],
    )
    def test_settled_requires_cost_and_outcome(
        self, actual: int | None, failed: bool | None
    ) -> None:
        with pytest.raises(ConfirmationPolicyError, match="settled reservation"):
            BudgetReservation(
                reservation_id="r",
                bundle_key=_KEY,
                utc_day=_DAY,
                reserved_microusd=100,
                state=ReservationState.SETTLED,
                actual_microusd=actual,
                failed_attempt=failed,
            )

    def test_reservation_is_frozen(self) -> None:
        reservation = BudgetReservation(
            reservation_id="r",
            bundle_key=_KEY,
            utc_day=_DAY,
            reserved_microusd=100,
        )
        with pytest.raises(FrozenInstanceError):
            reservation.state = ReservationState.SETTLED  # type: ignore[misc]


class TestReservationArithmetic:
    def test_first_reservation_increments_attempt_revision_and_outstanding(
        self,
    ) -> None:
        before = DailyBudgetSnapshot(utc_day=_DAY)
        after, reservation = reserve(before, amount=100)
        assert after.revision == 1
        assert after.issued_attempts == 1
        assert after.outstanding_reserved_microusd == 100
        assert after.settled_microusd == 0
        assert reservation.utc_day == _DAY
        assert before == DailyBudgetSnapshot(utc_day=_DAY)

    def test_zero_reservation_is_forbidden_to_prevent_dollar_cap_bypass(self) -> None:
        with pytest.raises(ConfirmationPolicyError, match="reserve_microusd"):
            reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=0)

    @pytest.mark.parametrize("amount", [-1, True, 1.0, (1 << 62) + 1])
    def test_rejects_invalid_reservation_amount(self, amount: object) -> None:
        with pytest.raises(ConfirmationPolicyError, match="reserve_microusd"):
            reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=amount)  # type: ignore[arg-type]

    def test_exact_dollar_cap_is_allowed(self) -> None:
        after, _ = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=1_000)
        assert after.committed_microusd == 1_000

    def test_one_microusd_past_dollar_cap_is_rejected(self) -> None:
        with pytest.raises(BudgetExhausted) as exc:
            reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=1_001)
        assert exc.value.reason == "dollar_cap"

    def test_existing_settled_spend_counts_against_dollar_cap(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY, settled_microusd=950)
        with pytest.raises(BudgetExhausted) as exc:
            reserve(snapshot, amount=51)
        assert exc.value.reason == "dollar_cap"

    def test_existing_outstanding_reservation_counts_against_dollar_cap(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY, outstanding_reserved_microusd=950)
        with pytest.raises(BudgetExhausted) as exc:
            reserve(snapshot, amount=51)
        assert exc.value.reason == "dollar_cap"

    def test_settled_and_outstanding_both_count_against_cap(self) -> None:
        snapshot = DailyBudgetSnapshot(
            utc_day=_DAY,
            outstanding_reserved_microusd=400,
            settled_microusd=500,
        )
        after, _ = reserve(snapshot, amount=100)
        assert after.committed_microusd == 1_000
        with pytest.raises(BudgetExhausted):
            reserve(after, amount=1, reservation_id="next")

    def test_exact_bundle_cap_is_allowed(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY, issued_attempts=4)
        after, _ = reserve(snapshot, amount=1)
        assert after.issued_attempts == 5

    def test_bundle_cap_stops_next_attempt_even_with_dollar_headroom(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY, issued_attempts=5)
        with pytest.raises(BudgetExhausted) as exc:
            reserve(snapshot, amount=1)
        assert exc.value.reason == "bundle_cap"

    def test_bundle_cap_is_checked_before_dollar_cap(self) -> None:
        snapshot = DailyBudgetSnapshot(
            utc_day=_DAY,
            issued_attempts=5,
            settled_microusd=1_000,
        )
        with pytest.raises(BudgetExhausted) as exc:
            reserve(snapshot, amount=1)
        assert exc.value.reason == "bundle_cap"

    def test_failed_attempt_still_consumes_bundle_count(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        snapshot, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=25,
            failed_attempt=True,
        )
        assert settled.failed_attempt is True
        assert snapshot.issued_attempts == 1
        assert snapshot.settled_microusd == 25

    def test_policy_off_refuses_issuance(self) -> None:
        policy = ConfirmationBundleSettings()
        with pytest.raises(ConfirmationPolicyError, match="policy is off"):
            reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=1, policy=policy)

    def test_shadow_and_enforce_use_same_cost_bounds(self) -> None:
        shadow, _ = reserve(
            DailyBudgetSnapshot(utc_day=_DAY),
            amount=500,
            policy=settings(mode=ConfirmationMode.SHADOW),
        )
        enforce, _ = reserve(
            DailyBudgetSnapshot(utc_day=_DAY),
            amount=500,
            policy=settings(mode=ConfirmationMode.ENFORCE),
        )
        assert shadow == enforce


class TestCompareAndSwapInvariants:
    def test_stale_reservation_revision_is_rejected(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY, revision=7)
        with pytest.raises(StaleBudgetSnapshot, match="expected 6, found 7"):
            reserve_daily_budget(
                snapshot,
                expected_revision=6,
                settings=settings(),
                bundle_key=_KEY,
                reservation_id="r",
                reserve_microusd=100,
            )

    def test_two_decisions_from_same_snapshot_have_same_cas_revision(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        first, _ = reserve(snapshot, amount=600, reservation_id="first")
        competing, _ = reserve(snapshot, amount=600, reservation_id="competing")
        assert first.revision == competing.revision == 1
        # Persistence may apply only one update WHERE revision=0. Re-evaluating
        # the loser against the winner now observes the dollar cap.
        with pytest.raises(BudgetExhausted):
            reserve(first, amount=600, reservation_id="retry")

    def test_replaying_old_expected_revision_after_success_is_rejected(self) -> None:
        snapshot, _ = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        with pytest.raises(StaleBudgetSnapshot):
            reserve_daily_budget(
                snapshot,
                expected_revision=0,
                settings=settings(),
                bundle_key=_KEY,
                reservation_id="replay",
                reserve_microusd=100,
            )

    def test_stale_settlement_revision_is_rejected(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        with pytest.raises(StaleBudgetSnapshot):
            settle_daily_budget(
                snapshot,
                reservation,
                expected_revision=0,
                actual_microusd=50,
                failed_attempt=False,
            )

    def test_sequential_reservations_get_monotonic_revisions(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        for index in range(1, 6):
            snapshot, _ = reserve(
                snapshot,
                amount=100,
                reservation_id=f"reservation-{index}",
            )
            assert snapshot.revision == index
            assert snapshot.issued_attempts == index
        assert snapshot.committed_microusd == 500


class TestSettlementArithmetic:
    def test_settlement_releases_reservation_and_books_actual(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        after, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=75,
            failed_attempt=False,
        )
        assert after.revision == snapshot.revision + 1
        assert after.outstanding_reserved_microusd == 0
        assert after.settled_microusd == 75
        assert after.issued_attempts == 1
        assert settled.state == ReservationState.SETTLED
        assert settled.actual_microusd == 75
        assert settled.failed_attempt is False

    def test_zero_actual_cost_is_valid_for_failed_or_synthetic_attempt(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        after, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=0,
            failed_attempt=True,
        )
        assert after.settled_microusd == 0
        assert after.outstanding_reserved_microusd == 0
        assert settled.actual_microusd == 0

    def test_actual_below_reservation_frees_unused_headroom(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=800)
        snapshot, _ = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=300,
            failed_attempt=False,
        )
        snapshot, _ = reserve(snapshot, amount=700, reservation_id="second")
        assert snapshot.committed_microusd == 1_000

    def test_actual_above_reservation_is_accepted_and_visible(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=900)
        after, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=1_100,
            failed_attempt=False,
        )
        assert after.settled_microusd == 1_100
        assert after.committed_microusd == 1_100
        assert settled.actual_microusd == 1_100

    def test_settlement_overflow_blocks_only_future_issuance(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=900)
        snapshot, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=1_100,
            failed_attempt=False,
        )
        assert settled.state == ReservationState.SETTLED
        assert settled.failed_attempt is False
        with pytest.raises(BudgetExhausted) as exc:
            reserve(snapshot, amount=1, reservation_id="future")
        assert exc.value.reason == "dollar_cap"

    def test_accepted_evidence_settles_after_another_attempt_exhausts_cap(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        snapshot, first = reserve(snapshot, amount=500, reservation_id="first")
        snapshot, second = reserve(snapshot, amount=500, reservation_id="second")
        with pytest.raises(BudgetExhausted):
            reserve(snapshot, amount=1, reservation_id="third")
        # Both already-issued leases remain acceptable after exhaustion.
        snapshot, first_done = settle_daily_budget(
            snapshot,
            first,
            expected_revision=snapshot.revision,
            actual_microusd=450,
            failed_attempt=False,
        )
        snapshot, second_done = settle_daily_budget(
            snapshot,
            second,
            expected_revision=snapshot.revision,
            actual_microusd=550,
            failed_attempt=False,
        )
        assert first_done.state == ReservationState.SETTLED
        assert second_done.state == ReservationState.SETTLED
        assert snapshot.settled_microusd == 1_000
        assert snapshot.outstanding_reserved_microusd == 0

    def test_late_settlement_must_update_original_day_not_current_day(self) -> None:
        old, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        next_day = DailyBudgetSnapshot(utc_day=_DAY + timedelta(days=1))
        with pytest.raises(ConfirmationPolicyError, match="original UTC-day"):
            settle_daily_budget(
                next_day,
                reservation,
                expected_revision=next_day.revision,
                actual_microusd=80,
                failed_attempt=False,
            )
        old, settled = settle_daily_budget(
            old,
            reservation,
            expected_revision=old.revision,
            actual_microusd=80,
            failed_attempt=False,
        )
        assert settled.utc_day == _DAY
        assert old.settled_microusd == 80

    def test_cannot_settle_same_reservation_twice(self) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        snapshot, settled = settle_daily_budget(
            snapshot,
            reservation,
            expected_revision=snapshot.revision,
            actual_microusd=80,
            failed_attempt=False,
        )
        with pytest.raises(ConfirmationPolicyError, match="already settled"):
            settle_daily_budget(
                snapshot,
                settled,
                expected_revision=snapshot.revision,
                actual_microusd=80,
                failed_attempt=False,
            )

    def test_cannot_settle_reservation_missing_from_snapshot(self) -> None:
        reservation = BudgetReservation(
            reservation_id="missing",
            bundle_key=_KEY,
            utc_day=_DAY,
            reserved_microusd=100,
        )
        with pytest.raises(ConfirmationPolicyError, match="missing the reservation"):
            settle_daily_budget(
                DailyBudgetSnapshot(utc_day=_DAY),
                reservation,
                expected_revision=0,
                actual_microusd=0,
                failed_attempt=True,
            )

    @pytest.mark.parametrize("actual", [-1, True, 1.0, (1 << 62) + 1])
    def test_rejects_invalid_actual_cost(self, actual: object) -> None:
        snapshot, reservation = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=100)
        with pytest.raises(ConfirmationPolicyError, match="actual_microusd"):
            settle_daily_budget(
                snapshot,
                reservation,
                expected_revision=snapshot.revision,
                actual_microusd=actual,  # type: ignore[arg-type]
                failed_attempt=False,
            )

    def test_multiple_reservations_settle_without_losing_each_other(self) -> None:
        snapshot = DailyBudgetSnapshot(utc_day=_DAY)
        snapshot, first = reserve(snapshot, amount=300, reservation_id="first")
        snapshot, second = reserve(snapshot, amount=400, reservation_id="second")
        snapshot, third = reserve(snapshot, amount=300, reservation_id="third")
        assert snapshot.outstanding_reserved_microusd == 1_000

        snapshot, _ = settle_daily_budget(
            snapshot,
            second,
            expected_revision=snapshot.revision,
            actual_microusd=350,
            failed_attempt=False,
        )
        assert snapshot.outstanding_reserved_microusd == 600
        assert snapshot.settled_microusd == 350

        snapshot, _ = settle_daily_budget(
            snapshot,
            first,
            expected_revision=snapshot.revision,
            actual_microusd=250,
            failed_attempt=True,
        )
        assert snapshot.outstanding_reserved_microusd == 300
        assert snapshot.settled_microusd == 600

        snapshot, _ = settle_daily_budget(
            snapshot,
            third,
            expected_revision=snapshot.revision,
            actual_microusd=300,
            failed_attempt=False,
        )
        assert snapshot.outstanding_reserved_microusd == 0
        assert snapshot.settled_microusd == 900
        assert snapshot.issued_attempts == 3

    def test_settlement_returns_new_objects(self) -> None:
        original = DailyBudgetSnapshot(utc_day=_DAY)
        reserved, reservation = reserve(original, amount=100)
        settled_snapshot, settled_reservation = settle_daily_budget(
            reserved,
            reservation,
            expected_revision=reserved.revision,
            actual_microusd=50,
            failed_attempt=False,
        )
        assert original.committed_microusd == 0
        assert reserved.outstanding_reserved_microusd == 100
        assert reservation.state == ReservationState.RESERVED
        assert settled_snapshot.settled_microusd == 50
        assert settled_reservation.state == ReservationState.SETTLED


class TestAttemptAccountingAcrossDays:
    def test_failed_attempt_yesterday_does_not_consume_today_bundle_count(self) -> None:
        yesterday = DailyBudgetSnapshot(utc_day=_DAY)
        yesterday, reservation = reserve(yesterday, amount=100)
        yesterday, _ = settle_daily_budget(
            yesterday,
            reservation,
            expected_revision=yesterday.revision,
            actual_microusd=20,
            failed_attempt=True,
        )
        today = DailyBudgetSnapshot(utc_day=_DAY + timedelta(days=1))
        today, _ = reserve(today, amount=1_000, reservation_id="today")
        assert yesterday.issued_attempts == 1
        assert today.issued_attempts == 1

    def test_outstanding_yesterday_does_not_reserve_today_cap(self) -> None:
        yesterday, old = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=1_000)
        today = DailyBudgetSnapshot(utc_day=_DAY + timedelta(days=1))
        today, _ = reserve(today, amount=1_000, reservation_id="today")
        assert yesterday.outstanding_reserved_microusd == 1_000
        assert today.outstanding_reserved_microusd == 1_000
        assert old.utc_day != today.utc_day

    def test_late_yesterday_overflow_does_not_corrupt_today_row(self) -> None:
        yesterday, old = reserve(DailyBudgetSnapshot(utc_day=_DAY), amount=900)
        today = DailyBudgetSnapshot(utc_day=_DAY + timedelta(days=1))
        today, _ = reserve(today, amount=500, reservation_id="today")
        yesterday, _ = settle_daily_budget(
            yesterday,
            old,
            expected_revision=yesterday.revision,
            actual_microusd=1_100,
            failed_attempt=False,
        )
        assert yesterday.settled_microusd == 1_100
        assert today.committed_microusd == 500
