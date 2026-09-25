"""The one rule that decides why a submission is under review right now."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from ditto.api_server.ath_review_state import (
    DEFAULT_OPEN_REASON,
    DEFAULT_RESOLVED_REASON,
    derive_ath_review_lifecycle,
)
from ditto.db.models import AthReview, AthReviewAction

_T0 = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
_I5_HOLD = "Deferred source review qualified this submission for I5 inspection"
_I5_REJECT = "Reject under policy v13 for I5: benchmark-shaped answer construction"
_REOPEN = "I5 rejection withdrawn as unsupported; reconsidering under v13 checks"


def _review(review_id: UUID, **kwargs: object) -> AthReview:
    defaults: dict[str, object] = {
        "review_id": review_id,
        "agent_id": uuid4(),
        "status": "pending",
        "opened_at": _T0,
        "reopened_at": None,
        "resolved_at": None,
        "resolved_by": None,
        "resolution": None,
        "resolution_reason": None,
        "original_duplicate_of": None,
        "original_reason": _I5_HOLD,
        "original_policy_version": 13,
        "original_evidence": {},
        "algorithm_provenance": {},
    }
    return AthReview(**{**defaults, **kwargs})


def _action(review_id: UUID, action: str, reason: str, at: datetime) -> AthReviewAction:
    return AthReviewAction(
        action_id=uuid4(),
        review_id=review_id,
        action=action,
        reason=reason,
        actor="operator",
        evidence={},
        created_at=at,
    )


def test_open_hold_reports_the_reason_it_was_opened_with() -> None:
    review_id = uuid4()
    lifecycle = derive_ath_review_lifecycle(
        _review(review_id), latest_action=None, actions=[]
    )
    assert lifecycle.event == "opened"
    assert lifecycle.reason_source == "original_hold"
    assert lifecycle.reason == _I5_HOLD
    assert lifecycle.superseded_reason is None
    assert lifecycle.superseded_resolution is None
    assert lifecycle.superseded_resolution_reason is None
    assert lifecycle.superseded_at is None


def test_active_rejection_still_reads_as_the_rejection() -> None:
    """The ordinary terminal case must be untouched by the reopen projection."""
    review_id = uuid4()
    resolved_at = _T0 + timedelta(hours=1)
    lifecycle = derive_ath_review_lifecycle(
        _review(
            review_id,
            status="resolved",
            resolved_at=resolved_at,
            resolved_by="operator",
            resolution="reject",
            resolution_reason=_I5_REJECT,
        ),
        latest_action=(reject := _action(review_id, "reject", _I5_REJECT, resolved_at)),
        actions=[reject],
    )
    assert lifecycle.event == "rejected"
    assert lifecycle.reason == _I5_REJECT
    assert lifecycle.reason_source == "original_hold"
    assert lifecycle.superseded_reason is None
    assert lifecycle.superseded_resolution is None


def test_withdrawn_rejection_makes_the_reconsideration_the_active_reason() -> None:
    review_id = uuid4()
    rejected_at = _T0 + timedelta(hours=1)
    reopened_at = _T0 + timedelta(hours=2)
    # Exactly the row shape a guarded reopen leaves behind: the resolution and
    # its reason are NULLed to satisfy ath_reviews_lifecycle_check, and only
    # the append-only ledger still holds the withdrawn reject prose.
    reject = _action(review_id, "reject", _I5_REJECT, rejected_at)
    reopen = _action(review_id, "reopen", _REOPEN, reopened_at)
    lifecycle = derive_ath_review_lifecycle(
        _review(review_id, status="pending", reopened_at=reopened_at),
        latest_action=reopen,
        actions=[reject, reopen],
    )
    assert lifecycle.event == "reopened"
    assert lifecycle.reason_source == "reconsideration"
    assert lifecycle.reason == _REOPEN
    assert lifecycle.superseded_reason == _I5_HOLD
    assert lifecycle.superseded_resolution == "reject"
    assert lifecycle.superseded_resolution_reason == _I5_REJECT
    assert lifecycle.superseded_at == reopened_at
    assert lifecycle.reason != _I5_REJECT


def test_reconsideration_reason_holds_without_the_full_ledger() -> None:
    """The public page loads only the newest action and must still be right."""
    review_id = uuid4()
    reopened_at = _T0 + timedelta(hours=2)
    lifecycle = derive_ath_review_lifecycle(
        _review(review_id, status="pending", reopened_at=reopened_at),
        latest_action=_action(review_id, "reopen", _REOPEN, reopened_at),
    )
    assert lifecycle.reason == _REOPEN
    assert lifecycle.reason_source == "reconsideration"
    assert lifecycle.superseded_reason == _I5_HOLD
    # Unknown, not "there was none": the caller did not read the ledger.
    assert lifecycle.superseded_resolution is None
    assert lifecycle.superseded_resolution_reason is None


def test_second_reconsideration_supersedes_the_newer_decision() -> None:
    review_id = uuid4()
    first_reject = _action(review_id, "reject", _I5_REJECT, _T0 + timedelta(hours=1))
    first_reopen = _action(
        review_id, "reopen", "first appeal", _T0 + timedelta(hours=2)
    )
    second_reject = _action(
        review_id, "reject", "Reject: second review upheld I5", _T0 + timedelta(hours=3)
    )
    second_reopen = _action(review_id, "reopen", _REOPEN, _T0 + timedelta(hours=4))
    lifecycle = derive_ath_review_lifecycle(
        _review(review_id, status="pending", reopened_at=second_reopen.created_at),
        latest_action=second_reopen,
        actions=[first_reject, first_reopen, second_reject, second_reopen],
    )
    assert lifecycle.reason == _REOPEN
    assert lifecycle.superseded_resolution_reason == "Reject: second review upheld I5"


def test_withdrawn_clear_is_labelled_as_a_clear_not_a_rejection() -> None:
    review_id = uuid4()
    clear = _action(
        review_id, "clear", "cleared on first pass", _T0 + timedelta(hours=1)
    )
    reopen = _action(review_id, "reopen", "new evidence", _T0 + timedelta(hours=2))
    lifecycle = derive_ath_review_lifecycle(
        _review(review_id, status="pending", reopened_at=reopen.created_at),
        latest_action=reopen,
        actions=[clear, reopen],
    )
    assert lifecycle.superseded_resolution == "clear"
    assert lifecycle.superseded_resolution_reason == "cleared on first pass"


def test_legacy_rows_without_stored_text_still_project_a_reason() -> None:
    review_id = uuid4()
    opened = derive_ath_review_lifecycle(
        _review(review_id, original_reason=None), latest_action=None
    )
    assert opened.reason == DEFAULT_OPEN_REASON
    resolved = derive_ath_review_lifecycle(
        _review(
            review_id,
            status="resolved",
            resolved_at=_T0,
            resolved_by="operator",
            resolution="clear",
            resolution_reason=None,
            original_reason=None,
        ),
        latest_action=None,
    )
    assert resolved.event == "cleared"
    assert resolved.reason == DEFAULT_RESOLVED_REASON
