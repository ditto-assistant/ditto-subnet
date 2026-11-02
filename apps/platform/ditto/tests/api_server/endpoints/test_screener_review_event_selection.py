"""Which accepted screener verdicts append an automated review event.

Pure predicate tests: no database. The end-to-end persistence path is covered
by ``test_screener.py::TestSubmitResult``.
"""

from __future__ import annotations

import pytest

from ditto.api_server.endpoints.screener import _appends_automated_review_event


def test_reviewed_pass_appends_review_event_including_policy_only_rescreen() -> None:
    # A full screen and a policy-only rescreen both run source review. Neither
    # is a build-only attempt, so both clean passes keep their audit row
    # (#2672: scored agents cleared by a v13 rescreen had none).
    assert _appends_automated_review_event(
        records_review_evidence=False,
        deferred_deep_hold=False,
        outcome_value="pass",
        build_only=False,
    )


def test_build_only_pass_without_review_evidence_appends_no_review_event() -> None:
    # The mechanical lane skips source review; there is nothing to snapshot.
    assert not _appends_automated_review_event(
        records_review_evidence=False,
        deferred_deep_hold=False,
        outcome_value="pass",
        build_only=True,
    )


@pytest.mark.parametrize(
    ("records_review_evidence", "deferred_deep_hold", "outcome_value"),
    [
        (True, False, "quarantine"),
        (True, False, "pass_inconclusive"),
        (True, False, "pass"),
        (False, True, "retryable_infra"),
    ],
)
@pytest.mark.parametrize("build_only", [False, True])
def test_review_evidence_or_deferred_hold_always_appends_review_event(
    records_review_evidence: bool,
    deferred_deep_hold: bool,
    outcome_value: str,
    build_only: bool,
) -> None:
    assert _appends_automated_review_event(
        records_review_evidence=records_review_evidence,
        deferred_deep_hold=deferred_deep_hold,
        outcome_value=outcome_value,
        build_only=build_only,
    )


@pytest.mark.parametrize(
    "outcome_value", ["deterministic_reject", "retryable_infra", "inconclusive", None]
)
def test_unreviewed_non_pass_appends_no_review_event(outcome_value: str | None) -> None:
    assert not _appends_automated_review_event(
        records_review_evidence=False,
        deferred_deep_hold=False,
        outcome_value=outcome_value,
        build_only=False,
    )
