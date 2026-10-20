"""Operator reads of the bounty engine: private audit, review and settlement queues.

Public views deliberately hide who decided what. Operators need exactly that
to run and audit the workflow, and Backroom must see every control and its
last applied state without the UI (repo rule: err toward more visibility).
These reads are admin-only and change nothing:

- one bounty's operator view: the public detail plus the reviewer in force,
  every revision's private fields, every claim, appeal statements, and one
  chronological audit of every operator record with its actor and reason;
- the review queue: submissions awaiting a decision, overdue first;
- the settlement queue: obligations that still need an operator, with the
  operators who approved their current round;
- the appeal queue: appeals awaiting a decision, the longest waiting first,
  each with its private statement and the public event it appeals.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, select, tuple_

from ditto.api_models.bounty import (
    BountyAdminAppealView,
    BountyAdminRevisionView,
    BountyAppealQueue,
    BountyAppealQueueItem,
    BountyAuditEntry,
    BountyOperatorView,
    BountyReviewQueue,
    BountyReviewQueueItem,
    BountySettlementItem,
    BountySettlementQueue,
    BountySubmissionView,
)
from ditto.api_server import bounty, bounty_claims
from ditto.api_server.bounty_claims import claim_views
from ditto.api_server.bounty_obligations import (
    current_policy,
    obligation_state,
    obligation_view,
)
from ditto.api_server.bounty_report import bounty_report
from ditto.api_server.bounty_reviews import current_reviewer
from ditto.db.models import (
    Bounty,
    BountyAppeal,
    BountyAppealDecision,
    BountyClaim,
    BountyClaimControl,
    BountyDeliveryFact,
    BountyEvent,
    BountyObligation,
    BountyObligationApproval,
    BountyReview,
    BountyReviewerAssignment,
    BountyRevision,
    BountySlot,
    BountySlotAction,
    BountySubmission,
    BountyTransfer,
    BountyTransferReconciliation,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Obligation states an operator still has to act on.
OPEN_SETTLEMENT_STATES = (
    "awaiting_approval",
    "over_policy_limit",
    "approved",
    "dispatched",
    "uncertain",
    "failed",
)


def _entry(
    kind: str,
    actor: str,
    reason: str | None,
    detail: str,
    at: datetime,
    *,
    claim_id: UUID | None = None,
    slot_id: UUID | None = None,
    obligation_id: UUID | None = None,
) -> BountyAuditEntry:
    return BountyAuditEntry(
        kind=kind,  # type: ignore[arg-type]
        actor=actor,
        reason=reason,
        detail=detail,
        recorded_at=at,
        claim_id=claim_id,
        slot_id=slot_id,
        obligation_id=obligation_id,
    )


async def _audit(
    session: AsyncSession, bounty_id: UUID, claim_ids: list[UUID]
) -> list[BountyAuditEntry]:
    entries: list[BountyAuditEntry] = []
    for revision in await session.scalars(
        select(BountyRevision).where(BountyRevision.bounty_id == bounty_id)
    ):
        entries.append(
            _entry(
                "revision_published",
                revision.actor,
                revision.reason,
                f"revision {revision.revision} {revision.spec_digest}",
                revision.published_at,
            )
        )
    for control in await session.scalars(
        select(BountyClaimControl).where(BountyClaimControl.bounty_id == bounty_id)
    ):
        entries.append(
            _entry(
                "claims_paused" if control.paused else "claims_resumed",
                control.actor,
                control.reason,
                "new claims paused" if control.paused else "new claims open",
                control.recorded_at,
            )
        )
    for assignment in await session.scalars(
        select(BountyReviewerAssignment).where(
            BountyReviewerAssignment.bounty_id == bounty_id
        )
    ):
        entries.append(
            _entry(
                "reviewer_assigned",
                assignment.actor,
                assignment.reason,
                f"reviewer {assignment.reviewer_actor}",
                assignment.assigned_at,
            )
        )
    for slot_action, slot in (
        await session.execute(
            select(BountySlotAction, BountySlot)
            .join(BountySlot, BountySlot.slot_id == BountySlotAction.slot_id)
            .where(BountySlot.bounty_id == bounty_id)
        )
    ).all():
        cancelled = slot_action.action == "cancel"
        entries.append(
            _entry(
                "slot_cancelled" if cancelled else "slot_reopened",
                slot_action.actor,
                slot_action.reason,
                (
                    f"round {slot.round} slot {slot.position}, was "
                    f"{slot_action.prior_status}"
                    + (f" ({slot_action.cancel_kind})" if cancelled else "")
                ),
                slot_action.recorded_at,
                claim_id=slot_action.claim_id,
                slot_id=slot.slot_id,
            )
        )
    if claim_ids:
        for review in await session.scalars(
            select(BountyReview).where(BountyReview.claim_id.in_(claim_ids))
        ):
            entries.append(
                _entry(
                    "review_decided",
                    review.reviewer_actor,
                    review.reason,
                    f"{review.decision} submission {review.submission_id}",
                    review.decided_at,
                    claim_id=review.claim_id,
                )
            )
        for appeal, decision in (
            await session.execute(
                select(BountyAppeal, BountyAppealDecision)
                .join(
                    BountyAppealDecision,
                    BountyAppealDecision.appeal_id == BountyAppeal.appeal_id,
                )
                .where(BountyAppeal.claim_id.in_(claim_ids))
            )
        ).all():
            entries.append(
                _entry(
                    "appeal_decided",
                    decision.actor,
                    decision.reason,
                    f"{'upheld' if decision.upheld else 'denied'} appeal "
                    f"{appeal.appeal_id}",
                    decision.decided_at,
                    claim_id=appeal.claim_id,
                )
            )
        for obligation in await session.scalars(
            select(BountyObligation).where(
                BountyObligation.claim_id.in_(claim_ids),
                BountyObligation.kind == "award",
            )
        ):
            assert obligation.actor is not None
            entries.append(
                _entry(
                    "award_recorded",
                    obligation.actor,
                    obligation.decision_reason,
                    f"{obligation.amount_alpha_rao} alpha rao",
                    obligation.created_at,
                    claim_id=obligation.claim_id,
                    obligation_id=obligation.obligation_id,
                )
            )
        for approval, owed in (
            await session.execute(
                select(BountyObligationApproval, BountyObligation)
                .join(
                    BountyObligation,
                    BountyObligation.obligation_id
                    == BountyObligationApproval.obligation_id,
                )
                .where(BountyObligation.claim_id.in_(claim_ids))
            )
        ).all():
            entries.append(
                _entry(
                    "obligation_approved",
                    approval.actor,
                    None,
                    f"round {approval.round} {approval.amount_alpha_rao} alpha rao to "
                    f"{approval.payee_coldkey} under policy {approval.policy_revision}",
                    approval.approved_at,
                    claim_id=owed.claim_id,
                    obligation_id=owed.obligation_id,
                )
            )
        for transfer, owed in (
            await session.execute(
                select(BountyTransfer, BountyObligation)
                .join(
                    BountyObligation,
                    BountyObligation.obligation_id == BountyTransfer.obligation_id,
                )
                .where(BountyObligation.claim_id.in_(claim_ids))
            )
        ).all():
            entries.append(
                _entry(
                    "transfer_dispatched",
                    transfer.actor,
                    None,
                    f"round {transfer.round} via {transfer.executor}: {transfer.state}",
                    transfer.dispatched_at,
                    claim_id=owed.claim_id,
                    obligation_id=owed.obligation_id,
                )
            )
        for reconciliation, transfer, owed in (
            await session.execute(
                select(BountyTransferReconciliation, BountyTransfer, BountyObligation)
                .join(
                    BountyTransfer,
                    BountyTransfer.transfer_id
                    == BountyTransferReconciliation.transfer_id,
                )
                .join(
                    BountyObligation,
                    BountyObligation.obligation_id == BountyTransfer.obligation_id,
                )
                .where(BountyObligation.claim_id.in_(claim_ids))
            )
        ).all():
            requested = (
                f" (asked {reconciliation.requested})"
                if reconciliation.requested
                else ""
            )
            entries.append(
                _entry(
                    "transfer_reconciled",
                    reconciliation.actor,
                    None,
                    f"transfer {transfer.transfer_id} round {transfer.round}: "
                    f"{reconciliation.state}{requested}",
                    reconciliation.recorded_at,
                    claim_id=owed.claim_id,
                    obligation_id=owed.obligation_id,
                )
            )
        for fact in await session.scalars(
            select(BountyDeliveryFact).where(BountyDeliveryFact.claim_id.in_(claim_ids))
        ):
            entries.append(
                _entry(
                    "fact_recorded",
                    fact.actor,
                    None,
                    f"{fact.kind} {fact.reference}",
                    fact.recorded_at,
                    claim_id=fact.claim_id,
                )
            )
    return sorted(entries, key=lambda entry: entry.recorded_at)


async def operator_view_for_issue(
    session: AsyncSession, *, repo: str, issue: int
) -> BountyOperatorView:
    """The operator view of the bounty for ``repo#issue``, read fresh."""
    bounty_id = await session.scalar(
        select(Bounty.bounty_id).where(
            Bounty.repo == repo, Bounty.issue_number == issue
        )
    )
    if bounty_id is None:
        raise HTTPException(404, "bounty not found")
    return await operator_view(session, bounty_id)


async def operator_view(session: AsyncSession, bounty_id: UUID) -> BountyOperatorView:
    detail = await bounty.detail(session, bounty_id)
    now = bounty_claims.utcnow()
    claims = list(
        await session.scalars(
            select(BountyClaim)
            .where(BountyClaim.bounty_id == bounty_id)
            .order_by(BountyClaim.held_at, BountyClaim.claim_id)
        )
    )
    claim_ids = [claim.claim_id for claim in claims]
    revisions = list(
        await session.scalars(
            select(BountyRevision)
            .where(BountyRevision.bounty_id == bounty_id)
            .order_by(BountyRevision.revision)
        )
    )
    appeals = (
        (
            await session.execute(
                select(BountyAppeal, BountyAppealDecision)
                .outerjoin(
                    BountyAppealDecision,
                    BountyAppealDecision.appeal_id == BountyAppeal.appeal_id,
                )
                .where(BountyAppeal.claim_id.in_(claim_ids))
                .order_by(BountyAppeal.filed_at)
            )
        ).all()
        if claim_ids
        else []
    )
    views = await claim_views(session, claims, now=now)
    appeal_views = [
        BountyAdminAppealView(
            appeal_id=appeal.appeal_id,
            claim_id=appeal.claim_id,
            subject_event_id=appeal.subject_event_id,
            statement=appeal.reason,
            reason_digest=appeal.reason_digest,
            filed_at=appeal.filed_at,
            upheld=decision.upheld if decision else None,
            decided_by=decision.actor if decision else None,
            decision_reason=decision.reason if decision else None,
            decided_at=decision.decided_at if decision else None,
            restored_deadline_at=decision.restored_deadline_at if decision else None,
        )
        for appeal, decision in appeals
    ]
    audit = await _audit(session, bounty_id, claim_ids)
    return BountyOperatorView(
        detail=detail,
        reviewer_actor=await current_reviewer(session, bounty_id),
        revisions=[
            BountyAdminRevisionView(
                revision=row.revision,
                spec_digest=row.spec_digest,
                reviewer_actor=row.reviewer_actor,
                actor=row.actor,
                reason=row.reason,
                published_at=row.published_at,
            )
            for row in revisions
        ],
        claims=views,
        appeals=appeal_views,
        audit=audit,
        report=bounty_report(
            detail=detail, claims=views, appeals=appeal_views, audit=audit
        ),
    )


def _due(slot: BountySlot) -> datetime:
    assert slot.review_due_at is not None
    return slot.review_due_at


async def review_queue(
    session: AsyncSession, *, overdue_only: bool = False
) -> BountyReviewQueue:
    """Submissions awaiting a decision, the most overdue first."""
    now = bounty_claims.utcnow()
    query = (
        select(BountySlot, BountyClaim, Bounty)
        .join(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
        .join(Bounty, Bounty.bounty_id == BountySlot.bounty_id)
        .where(BountySlot.status == "submitted")
        .order_by(BountySlot.review_due_at, BountySlot.slot_id)
    )
    if overdue_only:
        # Only the overdue rows are read at all.
        query = query.where(BountySlot.review_due_at <= now)
    queued = [
        (slot, claim, owner)
        for slot, claim, owner in (await session.execute(query)).tuples().all()
    ]
    # One read each for every item's latest submission and claimed terms.
    claim_ids = [claim.claim_id for _slot, claim, _owner in queued]
    latest: dict[UUID, BountySubmission] = {
        submission.claim_id: submission
        for submission in await session.scalars(
            select(BountySubmission)
            .where(BountySubmission.claim_id.in_(claim_ids))
            .order_by(BountySubmission.claim_id, BountySubmission.submitted_at.desc())
            .distinct(BountySubmission.claim_id)
        )
    }
    pairs = {(claim.bounty_id, claim.revision) for _slot, claim, _owner in queued}
    titles: dict[tuple[UUID, int], str] = {
        (bounty_id, number): title
        for bounty_id, number, title in (
            await session.execute(
                select(
                    BountyRevision.bounty_id,
                    BountyRevision.revision,
                    BountyRevision.title,
                ).where(
                    tuple_(BountyRevision.bounty_id, BountyRevision.revision).in_(pairs)
                )
            )
        ).all()
    }
    items: list[BountyReviewQueueItem] = []
    reviewers: dict[UUID, str] = {}
    for slot, claim, owner in queued:
        overdue = now >= _due(slot)
        submission = latest[claim.claim_id]
        if owner.bounty_id not in reviewers:
            reviewers[owner.bounty_id] = await current_reviewer(
                session, owner.bounty_id
            )
        items.append(
            BountyReviewQueueItem(
                bounty_id=owner.bounty_id,
                repo=owner.repo,
                issue=owner.issue_number,
                title=titles[(claim.bounty_id, claim.revision)],
                claim_id=claim.claim_id,
                slot_id=slot.slot_id,
                round=slot.round,
                position=slot.position,
                hotkey=claim.hotkey,
                github_login=claim.github_login,
                submission=BountySubmissionView(
                    submission_id=submission.submission_id,
                    pr_number=submission.pr_number,
                    head_sha=submission.head_sha,
                    submitted_at=submission.submitted_at,
                    # The slot's due date moves when a reviewer is reassigned.
                    review_due_at=slot.review_due_at,
                    decision=None,
                    decision_reason=None,
                    attribution=None,
                    rework_deadline_at=None,
                    decided_at=None,
                ),
                overdue=overdue,
                reviewer_actor=reviewers[owner.bounty_id],
            )
        )
    return BountyReviewQueue(items=items, total=len(items))


async def settlement_queue(
    session: AsyncSession, *, state: str | None = None
) -> BountySettlementQueue:
    """Obligations that still need an operator, oldest first."""
    wanted = (state,) if state else OPEN_SETTLEMENT_STATES
    policy = await current_policy(session)
    query = (
        select(BountyObligation, Bounty)
        .join(Bounty, Bounty.bounty_id == BountyObligation.bounty_id)
        .where(BountyObligation.kind != "fee_retained")
        .order_by(BountyObligation.created_at, BountyObligation.obligation_id)
    )
    if "confirmed" not in wanted:
        # Paid money is final and needs no operator: leave it out in SQL, so
        # the queue's cost follows what is open, not every obligation ever.
        query = query.where(
            ~exists().where(
                BountyTransfer.obligation_id == BountyObligation.obligation_id,
                BountyTransfer.state == "confirmed",
            )
        )
    rows = (await session.execute(query)).all()
    items: list[BountySettlementItem] = []
    for obligation, owner in rows:
        current = await obligation_state(session, obligation, policy)
        if current.state not in wanted:
            continue
        approvers = (
            list(
                await session.scalars(
                    select(BountyObligationApproval.actor)
                    .where(
                        BountyObligationApproval.obligation_id
                        == obligation.obligation_id,
                        BountyObligationApproval.round == current.round,
                        # A live transfer's approvers are those of the
                        # revision that authorized it.
                        BountyObligationApproval.policy_revision == current.revision,
                    )
                    .order_by(BountyObligationApproval.approved_at)
                )
            )
            if current.revision is not None
            else []
        )
        items.append(
            BountySettlementItem(
                bounty_id=owner.bounty_id,
                repo=owner.repo,
                issue=owner.issue_number,
                claim_id=obligation.claim_id,
                obligation=await obligation_view(
                    session, obligation, policy, state=current
                ),
                approvers=approvers,
            )
        )
    return BountySettlementQueue(items=items, total=len(items))


async def appeal_queue(session: AsyncSession) -> BountyAppealQueue:
    """Appeals awaiting a decision, the longest waiting first."""
    rows = (
        await session.execute(
            select(
                BountyAppeal,
                BountyEvent,
                BountyClaim,
                BountySlot,
                Bounty,
                BountyRevision.title,
            )
            .join(BountyEvent, BountyEvent.event_id == BountyAppeal.subject_event_id)
            .join(BountyClaim, BountyClaim.claim_id == BountyAppeal.claim_id)
            .join(BountySlot, BountySlot.slot_id == BountyClaim.slot_id)
            .join(Bounty, Bounty.bounty_id == BountyClaim.bounty_id)
            # The title of the terms the claim signed.
            .join(
                BountyRevision,
                (BountyRevision.bounty_id == BountyClaim.bounty_id)
                & (BountyRevision.revision == BountyClaim.revision),
            )
            .where(
                ~exists().where(
                    BountyAppealDecision.appeal_id == BountyAppeal.appeal_id
                )
            )
            .order_by(BountyAppeal.filed_at, BountyAppeal.appeal_id)
        )
    ).tuples()
    items = [
        BountyAppealQueueItem(
            appeal_id=appeal.appeal_id,
            claim_id=claim.claim_id,
            bounty_id=owner.bounty_id,
            repo=owner.repo,
            issue=owner.issue_number,
            title=title,
            slot_id=slot.slot_id,
            round=slot.round,
            position=slot.position,
            hotkey=claim.hotkey,
            github_login=claim.github_login,
            subject_event_id=subject.event_id,
            subject_kind=subject.kind,  # type: ignore[arg-type]
            subject_recorded_at=subject.recorded_at,
            statement=appeal.reason,
            reason_digest=appeal.reason_digest,
            filed_at=appeal.filed_at,
        )
        for appeal, subject, claim, slot, owner, title in rows
    ]
    return BountyAppealQueue(items=items, total=len(items))


async def require_bounty(session: AsyncSession, bounty_id: UUID) -> None:
    if await session.get(Bounty, bounty_id) is None:
        raise HTTPException(404, "bounty not found")
