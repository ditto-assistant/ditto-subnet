"""Which verdict-less screening attempts earn a bounded automatic retry."""

from __future__ import annotations

import pytest

from ditto.db.queries.screening_retry import automatic_review_retry_eligible
from ditto_screening_protocol import reason_codes


@pytest.mark.parametrize(
    ("reason_code", "outcome"),
    [
        ("l3-adjudicator-model-provider-fault", "retryable_infra"),
        ("l2-model-inconclusive", "inconclusive"),
        ("l3-critic-http-429", "retryable_infra"),
        ("l2-http-429", "retryable_infra"),
        ("l2-model-provider-fault", "retryable_infra"),
        ("l3-adjudicator-incomplete", "retryable_infra"),
        ("l3-adjudicator-model-step-budget", "retryable_infra"),
    ],
)
def test_worker_retryable_runs_are_eligible(reason_code: str, outcome: str) -> None:
    assert automatic_review_retry_eligible(reason_code, outcome)


@pytest.mark.parametrize(
    ("reason_code", "outcome"),
    [
        ("challenge-inconclusive", "inconclusive"),
        ("l2-causal-transition-category-mismatch", "inconclusive"),
        ("l3-critic-http-429", None),
        (None, "deterministic_reject"),
    ],
)
def test_other_outcomes_still_park(
    reason_code: str | None, outcome: str | None
) -> None:
    assert not automatic_review_retry_eligible(reason_code, outcome)


def test_fleet_backoff_codes_keep_their_own_retry_path() -> None:
    for code in reason_codes.INFRA_AUTO_RETRY_REASON_CODES:
        assert not automatic_review_retry_eligible(code, "retryable_infra")
