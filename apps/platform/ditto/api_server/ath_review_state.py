"""One rule for "why is this submission under review *right now*".

``ath_reviews`` keeps one row per agent for its whole life. ``original_reason``
is the reason that first routed the submission to review and is deliberately
immutable — lifecycle guards (``resolve_copy_review``) compare it against
``agents.review_reason`` and fail closed when they disagree. A reopen therefore
cannot overwrite it, and a reopen also has to NULL ``resolution`` /
``resolution_reason`` to satisfy ``ath_reviews_lifecycle_check``.

That leaves the append-only ``ath_review_actions`` ledger as the only place the
current state of a reopened hold is written down: the newest ``reopen`` action
carries the reconsideration reason, and the ``clear`` / ``reject`` action before
it carries the decision that was withdrawn.

The public activity projection already followed that ledger; the operator queue
and audit projections did not, so a submission whose rejection had been
withdrawn kept advertising the withdrawn rejection prose as its active hold
reason. Both now derive through here so the two surfaces cannot drift again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import AthReview, AthReviewAction

AthReviewEvent = Literal["opened", "reopened", "cleared", "rejected"]
AthReasonSource = Literal["original_hold", "reconsideration"]

DEFAULT_OPEN_REASON = "Submission routed to ATH review."
DEFAULT_RESOLVED_REASON = "ATH review resolved."

_RESOLUTION_ACTIONS = ("clear", "reject")


@dataclass(frozen=True, slots=True)
class AthReviewLifecycle:
    """The active reason for one ATH review, plus the history it superseded."""

    event: AthReviewEvent
    reason: str
    event_at: datetime
    opened_at: datetime
    reason_source: AthReasonSource
    superseded_reason: str | None = None
    """``original_reason`` once a reconsideration has taken over the active slot.

    Preserved verbatim — the reopen path never rewrites it — and returned only
    so a consumer can label it as history instead of losing it.
    """

    superseded_resolution: Literal["clear", "reject"] | None = None
    superseded_resolution_reason: str | None = None
    """The withdrawn decision's own public reason, read back from the ledger.

    ``ath_reviews.resolution_reason`` is NULLed by the reopen, so a pending
    reopened review has no other durable copy of it.
    """

    superseded_at: datetime | None = None


def derive_ath_review_lifecycle(
    review: AthReview,
    *,
    latest_action: AthReviewAction | None,
    actions: Sequence[AthReviewAction] | None = None,
) -> AthReviewLifecycle:
    """Project one review's current lifecycle state.

    ``latest_action`` is the newest action on the review and is all that the
    active ``event`` / ``reason`` need, so the public activity page can keep its
    single "newest action per review" query. Pass the full ordered ``actions``
    (oldest first) as well to also recover the decision a reopen withdrew.
    """
    opened_at = review.reopened_at or review.opened_at
    if review.status == "pending":
        if latest_action is not None and latest_action.action == "reopen":
            return AthReviewLifecycle(
                event="reopened",
                reason=latest_action.reason,
                event_at=latest_action.created_at,
                opened_at=opened_at,
                reason_source="reconsideration",
                superseded_reason=review.original_reason,
                superseded_resolution=_withdrawn_resolution(actions, latest_action),
                superseded_resolution_reason=_withdrawn_reason(actions, latest_action),
                superseded_at=latest_action.created_at,
            )
        return AthReviewLifecycle(
            event="opened",
            reason=review.original_reason or DEFAULT_OPEN_REASON,
            event_at=review.opened_at,
            opened_at=opened_at,
            reason_source="original_hold",
        )

    resolution = review.resolution or (
        latest_action.action if latest_action is not None else None
    )
    return AthReviewLifecycle(
        event="rejected" if resolution == "reject" else "cleared",
        reason=(
            review.resolution_reason
            or (latest_action.reason if latest_action is not None else None)
            or DEFAULT_RESOLVED_REASON
        ),
        event_at=(
            review.resolved_at
            or (latest_action.created_at if latest_action is not None else None)
            or review.opened_at
        ),
        opened_at=opened_at,
        reason_source="original_hold",
    )


def _withdrawn_action(
    actions: Sequence[AthReviewAction] | None, reopen: AthReviewAction
) -> AthReviewAction | None:
    """The newest clear/reject recorded before ``reopen`` — what it withdrew."""
    if not actions:
        return None
    withdrawn: AthReviewAction | None = None
    for action in actions:
        if (action.created_at, action.action_id) >= (
            reopen.created_at,
            reopen.action_id,
        ):
            break
        if action.action in _RESOLUTION_ACTIONS:
            withdrawn = action
    return withdrawn


def _withdrawn_resolution(
    actions: Sequence[AthReviewAction] | None, reopen: AthReviewAction
) -> Literal["clear", "reject"] | None:
    withdrawn = _withdrawn_action(actions, reopen)
    if withdrawn is None:
        return None
    return "reject" if withdrawn.action == "reject" else "clear"


def _withdrawn_reason(
    actions: Sequence[AthReviewAction] | None, reopen: AthReviewAction
) -> str | None:
    withdrawn = _withdrawn_action(actions, reopen)
    return withdrawn.reason if withdrawn is not None else None


async def load_reopened_review_actions(
    session: AsyncSession, reviews: Sequence[AthReview]
) -> dict[UUID, list[AthReviewAction]]:
    """Batch-load the action ledger for the reviews that can have been reopened.

    ``reopened_at`` is written in the same transaction as the ``reopen`` action,
    so a review with no ``reopened_at`` provably has no reopen to project and is
    skipped — the queue's common case stays one extra query over a short id list
    (often none at all) rather than a ledger read for every row on the page.
    """
    review_ids = [
        review.review_id for review in reviews if review.reopened_at is not None
    ]
    if not review_ids:
        return {}
    grouped: dict[UUID, list[AthReviewAction]] = {}
    for action in await session.scalars(
        select(AthReviewAction)
        .where(AthReviewAction.review_id.in_(review_ids))
        .order_by(
            AthReviewAction.review_id,
            AthReviewAction.created_at,
            AthReviewAction.action_id,
        )
    ):
        grouped.setdefault(action.review_id, []).append(action)
    return grouped
