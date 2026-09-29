"""Manual ATH hold withdrawal rules; rewards use the canonical evaluator.

The withdraw endpoint and the operator audit read both decide "can this hold be
withdrawn right now" through :func:`withdrawal_refusal`, so Backroom never
offers a withdrawal that Platform would then refuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from ditto.api_models.agent_status import AgentStatus

if TYPE_CHECKING:
    from ditto.api_models.emission_eligibility import AgentEmissionEligibility
    from ditto.db.models import Agent, AthReview, AthReviewAction

MANUAL_HOLD_SNAPSHOT = "manual-admin-hold"
WITHDRAW_CONFIRMATION = "WITHDRAW ATH HOLD"

TERMINAL_RULING_ACTIONS = frozenset({"clear", "reject"})
"""Ledger actions that are rulings on the artifact.

A ``withdraw`` is not one of them: it retracts a hold without ruling, so a
withdrawn hold that is reopened can be withdrawn again.
"""

PRIOR_RULING_REFUSAL = (
    "review has a prior clear/reject ruling; resolve it with clear or reject"
)
"""Why a reopened ruling cannot leave through ``withdraw``.

Withdrawing a reopened reject would restore a banned artifact to ``scored``
and turn the reject into a ``withdraw`` that precedent search omits.
Withdrawing a precautionary re-hold of a cleared artifact would replace a
terminal certification with an incomplete review, which ``enforce`` withholds.
"""

_WITHDRAWN_PREFIX = (
    "Hold withdrawn without a misconduct finding or completed certification."
)


def is_manual_precautionary_hold(provenance: dict | None) -> bool:
    """True only for an operator-opened manual hold, not an automated gate."""
    return provenance is not None and provenance.get("snapshot") == MANUAL_HOLD_SNAPSHOT


def has_prior_terminal_ruling(actions: Sequence[AthReviewAction]) -> bool:
    """Whether any clear or reject was ever recorded on this review."""
    return any(action.action in TERMINAL_RULING_ACTIONS for action in actions)


def withdrawal_refusal(
    review: AthReview, agent: Agent, actions: Sequence[AthReviewAction]
) -> str | None:
    """The 409 detail that refuses a withdrawal now, or ``None`` if allowed.

    ``actions`` is the review's whole ledger. The artifact guards (SHA-256,
    score count, expected agent status) belong to the request and are checked
    by the endpoint, not here.
    """
    if review.status != "pending":
        if review.resolution == "withdraw":
            return "review already withdrawn"
        return "review already resolved"
    if agent.status != AgentStatus.ATH_PENDING_REVIEW:
        return "agent is no longer held"
    if not is_manual_precautionary_hold(review.algorithm_provenance):
        return "only a manual precautionary hold can be withdrawn"
    if has_prior_terminal_ruling(actions):
        return PRIOR_RULING_REFUSAL
    if agent.duplicate_of != review.original_duplicate_of:
        return "agent hold evidence no longer matches review"
    if agent.review_reason != review.original_reason:
        return "agent hold reason no longer matches review"
    return None


def lifecycle_version(
    review: AthReview, actions: Sequence[AthReviewAction]
) -> dict[str, Any]:
    """Identity of the review's current lifecycle, for binding a preview.

    Every reopen, clear, reject and withdraw appends to the ledger, so the
    action count and the newest action id change with each one. ``reopened_at``
    is written by the same transaction as a reopen.
    """
    latest = actions[-1] if actions else None
    return {
        "action_count": len(actions),
        "latest_action_id": str(latest.action_id) if latest is not None else None,
        "reopened_at": (
            review.reopened_at.isoformat() if review.reopened_at is not None else None
        ),
    }


def withdrawal_emission_reason(decision: AgentEmissionEligibility) -> str:
    """Operator-facing reward outcome of a withdrawal.

    The canonical ``eligible`` sentence says review is terminal, which a
    withdrawal never is, so an eligible outcome gets withdrawal-specific text.
    """
    if decision.state == "eligible":
        if decision.enforcement == "off":
            return (
                f"{_WITHDRAWN_PREFIX} Reward eligibility is off, so the score "
                "earns emissions as it did before the hold."
            )
        return (
            f"{_WITHDRAWN_PREFIX} The current reward-eligibility policy does not "
            "withhold an incomplete review, so the score earns emissions."
        )
    if decision.reward_eligible:
        return (
            f"{_WITHDRAWN_PREFIX} The {decision.enforcement} policy records "
            f"{decision.state} without withholding, so the score still earns "
            "emissions."
        )
    if decision.state == "unresolved_review":
        return (
            f"{_WITHDRAWN_PREFIX} Emissions stay withheld as unresolved_review "
            "until the hold is reopened and cleared or rejected."
        )
    return f"{_WITHDRAWN_PREFIX} {decision.reason}"
