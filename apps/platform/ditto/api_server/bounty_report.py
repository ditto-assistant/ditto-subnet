"""One bounty's settlement report, derived from its operator view (#2944).

The report is source-safe: it names contributors, amounts and credit the
public views already show, and counts operator actions without naming the
operators. It reads only what the operator view reads, so the two never
disagree, and it checks the settlement invariants the database enforces, so a
rehearsal ends with evidence that each one held.

Its evaluation shows the current estimate and actual of each metric, kept
apart, and the current pilot decision with its integration status and
reasons. It leaves out who recorded them and their private notes and reasons.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from statistics import median

from ditto.api_models.bounty import (
    BountyAdminAppealView,
    BountyAuditEntry,
    BountyClaimView,
    BountyDetail,
    BountyEvaluationView,
    BountyObligationView,
    BountyReport,
    BountyReportAmount,
    BountyReportCheck,
    BountyReportClaim,
    BountyReportEvaluation,
    BountyReportFee,
    BountyReportMetrics,
)

# A fee refund for these reasons settles a paid fee. The other refund reasons
# return payments that never won their slot.
DISPOSITION_REFUNDS = ("accepted", "cancelled", "system_fault")
LIVE_TRANSFERS = ("dispatched", "uncertain", "confirmed")


def _amount(obligations: list[BountyObligationView]) -> BountyReportAmount:
    return BountyReportAmount(
        count=len(obligations),
        alpha_rao=sum(o.amount_alpha_rao for o in obligations),
    )


def _dispositions(claim: BountyClaimView) -> list[BountyObligationView]:
    return [
        o
        for o in claim.obligations
        if o.kind == "fee_retained"
        or (o.kind == "fee_refund" and o.reason in DISPOSITION_REFUNDS)
    ]


def _fee(claim: BountyClaimView) -> BountyReportFee:
    if claim.claimed_at is None:
        return "unpaid"
    found = _dispositions(claim)
    if not found:
        return "undecided"
    # One disposition per fee is an invariant the checks report. Should data
    # ever break it, money that moved outranks money owed, which outranks a
    # retention, whatever order the obligations are listed in.
    refunds = [o for o in found if o.kind == "fee_refund"]
    if any(o.state == "confirmed" for o in refunds):
        return "refunded"
    return "refund_owed" if refunds else "retained"


def _award(claim: BountyClaimView) -> BountyObligationView | None:
    return next((o for o in claim.obligations if o.kind == "award"), None)


def _decided_at(claim: BountyClaimView) -> datetime | None:
    decided = [s.decided_at for s in claim.submissions if s.decided_at is not None]
    return max(decided) if decided else None


def _claim(claim: BountyClaimView) -> BountyReportClaim:
    award = _award(claim)
    return BountyReportClaim(
        claim_id=claim.claim_id,
        github_login=claim.github_login,
        round=claim.round,
        position=claim.position,
        outcome=claim.slot_status if claim.state == "claimed" else claim.state,
        fee=_fee(claim),
        award_alpha_rao=award.amount_alpha_rao if award else 0,
        award_state=award.state if award else None,
        attribution=award.attribution if award else None,
        submissions=len(claim.submissions),
        claimed_at=claim.claimed_at,
        decided_at=_decided_at(claim),
    )


def _awards_within_pools(detail: BountyDetail, claims: list[BountyClaimView]) -> bool:
    """Whether each award kept the bounty's awards within its claim's pool.

    A claim is bounded by the pool of the revision it signed. Awards are
    recorded one at a time under the bounty's lock, so each one's pool covered
    every award recorded before it and its own amount. Awards recorded at the
    same instant count against each other, so a tie never hides an over-award.
    """
    pools = {r.revision: r.terms.reward_pool_alpha_rao for r in detail.revisions}
    awards = [(c.revision, award) for c in claims if (award := _award(c)) is not None]
    return all(
        sum(o.amount_alpha_rao for _, o in awards if o.created_at <= award.created_at)
        <= pools[revision]
        for revision, award in awards
    )


def _signed_pools(detail: BountyDetail, claims: list[BountyClaimView]) -> str:
    """The bounds ``_awards_within_pools`` checks: each awarded claim's pool."""
    pools = {r.revision: r.terms.reward_pool_alpha_rao for r in detail.revisions}
    signed = sorted({c.revision for c in claims if _award(c) is not None})
    return ", ".join(f"{pools[revision]} in revision {revision}" for revision in signed)


def _checks(
    detail: BountyDetail, claims: list[BountyClaimView]
) -> list[BountyReportCheck]:
    obligations = [o for claim in claims for o in claim.obligations]
    transfers = [t for o in obligations for t in o.transfers]
    paid = [claim for claim in claims if claim.claimed_at is not None]
    awards = [o for o in obligations if o.kind == "award"]
    awarded = sum(o.amount_alpha_rao for o in awards)
    confirmed = [t for t in transfers if t.state == "confirmed"]
    signed = _signed_pools(detail, claims)
    return [
        BountyReportCheck(
            name="awards_within_pool",
            ok=_awards_within_pools(detail, claims),
            # The bound checked is the pool each claim signed, which a later
            # revision never changes, so the current pool is not evidence.
            detail=(
                f"{awarded} alpha rao awarded, each bounded by the pool its claim "
                f"signed: {signed}"
                if signed
                else f"{awarded} alpha rao awarded"
            ),
        ),
        BountyReportCheck(
            name="awards_on_accepted_work",
            ok=all(
                claim.slot_status == "accepted"
                for claim in claims
                if _award(claim) is not None
            ),
            detail=f"{len(awards)} awards",
        ),
        BountyReportCheck(
            name="one_disposition_per_paid_fee",
            ok=all(len(_dispositions(claim)) <= 1 for claim in paid),
            detail=(
                f"{sum(len(_dispositions(claim)) for claim in paid)} dispositions "
                f"for {len(paid)} paid fees"
            ),
        ),
        BountyReportCheck(
            name="accepted_fees_refunded",
            ok=all(
                any(
                    o.kind == "fee_refund" and o.reason == "accepted"
                    for o in claim.obligations
                )
                for claim in paid
                if claim.state == "claimed" and claim.slot_status == "accepted"
            ),
            detail="each accepted claim is owed its entry fee back",
        ),
        BountyReportCheck(
            name="one_live_transfer_per_obligation",
            ok=all(
                sum(t.state in LIVE_TRANSFERS for t in o.transfers) <= 1
                for o in obligations
            ),
            detail=f"{len(transfers)} transfers for {len(obligations)} obligations",
        ),
        BountyReportCheck(
            name="confirmed_transfers_have_receipts",
            ok=all(t.receipt is not None for t in confirmed),
            detail=f"{len(confirmed)} confirmed transfers",
        ),
        BountyReportCheck(
            name="funding_mode_matches",
            ok=all(
                o.simulated == (claim.funding_mode == "rehearsal")
                and all(t.simulated == o.simulated for t in o.transfers)
                for claim in claims
                for o in claim.obligations
            ),
            detail="rehearsal money is simulated and live money is not",
        ),
    ]


def _evaluation(evaluation: BountyEvaluationView) -> BountyReportEvaluation:
    """The current values and decision; records are listed oldest first."""
    current: dict[str, dict[str, float]] = {"estimate": {}, "actual": {}}
    for entry in evaluation.entries:
        current[entry.kind][entry.metric] = entry.value
    decision = evaluation.decisions[-1] if evaluation.decisions else None
    return BountyReportEvaluation(
        estimates=BountyReportMetrics.model_validate(
            {
                name: current["estimate"].get(name)
                for name in BountyReportMetrics.model_fields
            }
        ),
        actuals=BountyReportMetrics.model_validate(
            {
                name: current["actual"].get(name)
                for name in BountyReportMetrics.model_fields
            }
        ),
        decision=decision.decision if decision else None,
        integration_status=decision.integration_status if decision else None,
        reasons=decision.reasons if decision else None,
        decided_at=decision.recorded_at if decision else None,
    )


def bounty_report(
    *,
    detail: BountyDetail,
    claims: list[BountyClaimView],
    appeals: list[BountyAdminAppealView],
    audit: list[BountyAuditEntry],
    evaluation: BountyEvaluationView,
) -> BountyReport:
    summary = detail.summary
    obligations = [o for claim in claims for o in claim.obligations]
    transfers = [t for o in obligations for t in o.transfers]
    paid = [claim for claim in claims if claim.claimed_at is not None]
    settled = [o for claim in paid for o in _dispositions(claim)]
    refunds = [o for o in settled if o.kind == "fee_refund"]
    returned = [
        o
        for o in obligations
        if o.kind == "fee_refund" and o.reason not in DISPOSITION_REFUNDS
    ]
    awards = [o for o in obligations if o.kind == "award"]
    submissions = [s for claim in claims for s in claim.submissions]
    review_seconds = [
        (s.decided_at - s.submitted_at).total_seconds()
        for s in submissions
        if s.decided_at is not None
    ]
    decided = [appeal.upheld for appeal in appeals if appeal.upheld is not None]
    return BountyReport(
        funding_mode=summary.funding_mode,
        slots=summary.slots,
        claims=[_claim(claim) for claim in claims],
        fees_paid=BountyReportAmount(
            count=len(paid), alpha_rao=sum(claim.fee_alpha_rao for claim in paid)
        ),
        fees_refunded=_amount([o for o in refunds if o.state == "confirmed"]),
        fees_refund_owed=_amount([o for o in refunds if o.state != "confirmed"]),
        fees_retained=_amount([o for o in settled if o.kind == "fee_retained"]),
        fees_undecided=BountyReportAmount(
            count=sum(not _dispositions(claim) for claim in paid),
            alpha_rao=sum(
                claim.fee_alpha_rao for claim in paid if not _dispositions(claim)
            ),
        ),
        payments_returned=_amount([o for o in returned if o.state == "confirmed"]),
        payments_return_owed=_amount([o for o in returned if o.state != "confirmed"]),
        reward_pool_alpha_rao=summary.reward_pool_alpha_rao,
        awarded=_amount(awards),
        awards_paid=_amount([o for o in awards if o.state == "confirmed"]),
        transfers=dict(Counter(t.state for t in transfers)),
        decisions=dict(
            Counter(s.decision for s in submissions if s.decision is not None)
        ),
        # Exact durations, rounded once to the nearest second.
        median_review_seconds=round(median(review_seconds)) if review_seconds else None,
        reviews_overdue=summary.reviews_overdue,
        appeals_filed=len(appeals),
        appeals_upheld=sum(decided),
        appeals_denied=len(decided) - sum(decided),
        facts=dict(Counter(f.kind for claim in claims for f in claim.facts)),
        operator_actions=len(audit),
        checks=_checks(detail, claims),
        evaluation=_evaluation(evaluation),
    )
