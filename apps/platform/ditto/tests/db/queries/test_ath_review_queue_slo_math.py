"""Pure age, breach, and classification math for the ATH-hold queue SLO.

These need no database. The Postgres-backed tests in
``test_ath_review_queue_slo.py`` prove the statement feeds these functions
the right rows.
"""

from __future__ import annotations

from typing import get_args
from uuid import uuid4

import pytest

from ditto.api_models.ath_review_queue_slo import (
    AthReviewHoldReason,
    AthReviewQueueClass,
)
from ditto.api_models.public import PublicAthReview
from ditto.db.queries.ath_review_queue_slo import (
    ATH_QUEUE_CLASSES,
    AthHoldReason,
    AthHoldRow,
    QueueAgeThresholds,
    ath_queue_class,
    build_ath_hold_row,
    build_ath_review_queue_slo_snapshot,
    classify_ath_hold_reason,
    normalize_review_kind,
    percentile_cont,
    summarize_ath_queue,
)


def _row(
    *,
    age_seconds: float,
    review_kind: str | None = "copy",
    trigger: str | None = None,
    agent_status: str = "ath_pending_review",
    attempt_status_since_hold: str | None = None,
) -> AthHoldRow:
    return build_ath_hold_row(
        review_id=uuid4(),
        agent_id=uuid4(),
        agent_status=agent_status,
        review_kind=review_kind,
        trigger=trigger,
        age_seconds=age_seconds,
        attempt_status_since_hold=attempt_status_since_hold,
    )


class TestWireContractStaysInStep:
    def test_query_and_wire_literals_match(self) -> None:
        assert get_args(AthReviewQueueClass) == ATH_QUEUE_CLASSES
        assert get_args(AthHoldReason) == get_args(AthReviewHoldReason)
        public_reason = PublicAthReview.model_fields["reason"].annotation
        assert set(get_args(public_reason)) == set(get_args(AthHoldReason))


class TestPercentileCont:
    def test_empty_is_none_not_zero(self) -> None:
        assert percentile_cont([], 0.5) is None

    def test_single_value(self) -> None:
        assert percentile_cont([42.0], 0.95) == 42.0

    @pytest.mark.parametrize(
        ("values", "fraction", "expected"),
        [
            # PostgreSQL percentile_cont: position = fraction * (n - 1).
            ([10.0, 20.0, 30.0, 40.0], 0.5, 25.0),
            ([10.0, 20.0, 30.0, 40.0], 0.95, 38.5),
            ([10.0, 20.0, 30.0], 0.5, 20.0),
            ([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0], 0.95, 10.5),
            ([5.0, 15.0], 0.0, 5.0),
            ([5.0, 15.0], 1.0, 15.0),
        ],
    )
    def test_matches_postgres_linear_interpolation(
        self, values: list[float], fraction: float, expected: float
    ) -> None:
        assert percentile_cont(values, fraction) == pytest.approx(expected)

    def test_rejects_out_of_range_fraction(self) -> None:
        with pytest.raises(ValueError):
            percentile_cont([1.0], 1.5)


class TestQueueClass:
    @pytest.mark.parametrize("raw", [None, "", "copy", "unknown_kind", 7])
    def test_missing_or_unknown_kind_is_copy(self, raw: object) -> None:
        # Same fallback as admin_copy_review._review_kind_filter.
        assert normalize_review_kind(raw) == "copy"
        assert ath_queue_class(raw, None) == "copy"

    def test_integrity_double_check_is_its_own_clock(self) -> None:
        assert (
            ath_queue_class("deferred_source_review", "integrity_double_check")
            == "integrity_double_check"
        )
        assert (
            ath_queue_class("deferred_source_review", "top_five")
            == "deferred_source_review"
        )

    def test_trigger_does_not_reclassify_other_kinds(self) -> None:
        assert ath_queue_class("copy", "integrity_double_check") == "copy"

    @pytest.mark.parametrize("kind", ["benchmark_overfit", "anomalous_score"])
    def test_known_kinds_pass_through(self, kind: str) -> None:
        assert ath_queue_class(kind, None) == kind


class TestReason:
    @pytest.mark.parametrize(
        "queue_class", ["copy", "benchmark_overfit", "anomalous_score"]
    )
    @pytest.mark.parametrize("attempt", [None, "running", "failed", "passed"])
    def test_operator_only_classes_are_always_escalation(
        self, queue_class: AthReviewQueueClass, attempt: str | None
    ) -> None:
        assert classify_ath_hold_reason(queue_class, attempt) == "escalation"

    @pytest.mark.parametrize(
        "queue_class", ["deferred_source_review", "integrity_double_check"]
    )
    @pytest.mark.parametrize(
        ("attempt", "reason"),
        [
            (None, "capacity_wait"),
            ("running", "active_work"),
            ("failed", "infrastructure_backoff"),
            ("expired", "infrastructure_backoff"),
            ("passed", "escalation"),
            ("rejected", "escalation"),
            ("quarantined", "escalation"),
        ],
    )
    def test_deep_pass_classes_follow_the_attempt_since_hold(
        self,
        queue_class: AthReviewQueueClass,
        attempt: str | None,
        reason: AthHoldReason,
    ) -> None:
        assert classify_ath_hold_reason(queue_class, attempt) == reason

    def test_stranded_agent_is_a_ghost_not_a_reason(self) -> None:
        row = _row(age_seconds=10.0, agent_status="scored")
        assert row.reason is None
        assert not row.actionable
        assert not row.stranded_terminal

    @pytest.mark.parametrize("status", ["banned", "rejected"])
    def test_terminal_agent_is_a_terminal_ghost(self, status: str) -> None:
        row = _row(age_seconds=10.0, agent_status=status)
        assert row.reason is None
        assert row.stranded_terminal

    def test_negative_clock_skew_clamps_to_zero(self) -> None:
        assert _row(age_seconds=-3.0).age_seconds == 0.0


class TestSummarize:
    def test_empty_backlog_reports_null_ages_and_zero_counts(self) -> None:
        stats = summarize_ath_queue(
            [],
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
        )
        assert stats.backlog_count == 0
        assert stats.p50_age_seconds is None
        assert stats.p95_age_seconds is None
        assert stats.oldest_age_seconds is None
        assert stats.oldest_agent_id is None
        assert stats.throughput_per_hour == 0.0

    def test_unset_thresholds_report_null_not_zero_or_healthy(self) -> None:
        stats = summarize_ath_queue(
            [_row(age_seconds=5.0)],
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
        )
        assert stats.max_actionable_age_threshold_seconds is None
        assert stats.overdue_count is None
        assert stats.p95_age_threshold_seconds is None
        assert stats.p95_exceeds_threshold is None

    def test_overdue_is_strictly_older_than_the_threshold(self) -> None:
        rows = [_row(age_seconds=age) for age in (59.0, 60.0, 61.0, 3600.0)]
        stats = summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
            thresholds=QueueAgeThresholds(max_actionable_age_seconds=60),
        )
        assert stats.overdue_count == 2

    def test_p95_breach_compares_the_interpolated_p95(self) -> None:
        rows = [_row(age_seconds=age) for age in (10.0, 20.0, 30.0, 40.0)]
        breached = summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
            thresholds=QueueAgeThresholds(p95_age_seconds=38),
        )
        healthy = summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
            thresholds=QueueAgeThresholds(p95_age_seconds=39),
        )
        assert breached.p95_age_seconds == pytest.approx(38.5)
        assert breached.p95_exceeds_threshold is True
        assert healthy.p95_exceeds_threshold is False

    def test_ghosts_never_count_toward_age_or_overdue(self) -> None:
        live = _row(age_seconds=100.0)
        rows = [
            live,
            _row(age_seconds=10_000_000.0, agent_status="banned"),
            _row(age_seconds=9_000_000.0, agent_status="scored"),
        ]
        stats = summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
            thresholds=QueueAgeThresholds(max_actionable_age_seconds=50),
        )
        assert stats.backlog_count == 1
        assert stats.oldest_age_seconds == 100.0
        assert stats.oldest_agent_id == live.agent_id
        assert stats.overdue_count == 1

    def test_oldest_names_the_queue_reason(self) -> None:
        rows = [
            _row(age_seconds=50.0),
            _row(
                age_seconds=500.0,
                review_kind="deferred_source_review",
                attempt_status_since_hold="failed",
            ),
        ]
        stats = summarize_ath_queue(
            rows,
            queue_class=None,
            throughput_completed_count=0,
            throughput_window_hours=24,
        )
        assert stats.oldest_reason == "infrastructure_backoff"
        assert stats.infrastructure_backoff_count == 1
        assert stats.escalation_count == 1

    def test_throughput_rate(self) -> None:
        stats = summarize_ath_queue(
            [],
            queue_class=None,
            throughput_completed_count=12,
            throughput_window_hours=24,
        )
        assert stats.throughput_per_hour == 0.5

    def test_rejects_non_positive_window(self) -> None:
        with pytest.raises(ValueError):
            summarize_ath_queue(
                [],
                queue_class=None,
                throughput_completed_count=0,
                throughput_window_hours=0,
            )


class TestSnapshot:
    def test_mixed_queue_splits_by_class_and_aggregates(self) -> None:
        rows = [
            _row(age_seconds=100.0, review_kind=None),  # legacy copy
            _row(age_seconds=200.0, review_kind="copy"),
            _row(
                age_seconds=300.0,
                review_kind="deferred_source_review",
                trigger="integrity_double_check",
                attempt_status_since_hold="running",
            ),
            _row(age_seconds=400.0, review_kind="anomalous_score"),
            _row(age_seconds=9_999.0, review_kind="copy", agent_status="rejected"),
            _row(age_seconds=8_888.0, review_kind="copy", agent_status="live"),
        ]
        snapshot = build_ath_review_queue_slo_snapshot(
            rows,
            throughput_by_class={"copy": 3, "anomalous_score": 1},
            held_without_review_ghost_count=2,
            ath_thresholds=QueueAgeThresholds(max_actionable_age_seconds=250),
            copy_thresholds=QueueAgeThresholds(max_actionable_age_seconds=150),
        )
        assert [stats.queue_class for stats in snapshot.classes] == list(
            ATH_QUEUE_CLASSES
        )
        assert snapshot.ath.backlog_count == 4
        assert snapshot.ath.overdue_count == 2
        assert snapshot.ath.oldest_age_seconds == 400.0
        assert snapshot.ath.throughput_completed_count == 4
        assert snapshot.ath.active_work_count == 1
        assert snapshot.ath.escalation_count == 3

        copy = snapshot.copy
        assert copy.backlog_count == 2
        assert copy.overdue_count == 1
        assert copy.max_actionable_age_threshold_seconds == 150
        assert copy.throughput_completed_count == 3
        assert copy.p50_age_seconds == pytest.approx(150.0)

        double_check = snapshot.for_class("integrity_double_check")
        assert double_check.backlog_count == 1
        assert double_check.active_work_count == 1
        # Only the ATH aggregate and the copy class take thresholds.
        assert double_check.overdue_count is None
        assert snapshot.for_class("benchmark_overfit").backlog_count == 0
        assert snapshot.for_class("benchmark_overfit").p50_age_seconds is None

        assert snapshot.stranded_terminal_ghost_count == 1
        assert snapshot.stranded_hold_ghost_count == 1
        assert snapshot.held_without_review_ghost_count == 2
        assert snapshot.ghost_count == 4
