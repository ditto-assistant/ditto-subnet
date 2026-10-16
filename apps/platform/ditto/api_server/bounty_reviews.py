"""Bounty work after the fee: submissions, review, appeals and slot control.

Contributor actions are signed by the claim's hotkey or by its payee of
record (never by whoever owns the hotkey now), each with a nonce from the
one table every bounty action shares, and each is an exact retry:

- **submit** the exact commit of a pull request while the slot is claimed or
  in rework, before its deadline. Platform records the signed PR and commit;
  the reviewer decides on that commit.
- **withdraw** a claimed or rework slot. The entry fee is retained.
- **appeal** a rejection or a missed deadline, once per decision.

Operator actions name the actor and need typed confirmations:

- the assigned reviewer **accepts**, **requests changes** with a recorded
  rework deadline, or **rejects** one exact submission;
- any operator **reassigns** the reviewer; overdue reviews get a new window;
- an operator other than the one who decided **upholds** or **denies** an
  appeal. Upholding restores the claim with a fresh delivery window;
- an operator **cancels** a slot (a paid fee is refunded in full) or
  **reopens** a closed one for a new claim.

Every action first applies the bounty's due deadlines under its lock.
Awards and fee obligations for decisions follow in P4.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ditto.api_models.bounty import (
    BountyAppealDecisionRequest,
    BountyAppealRequest,
    BountyCancelRequest,
    BountyClaimView,
    BountyReassignRequest,
    BountyReopenRequest,
    BountyReviewRequest,
    BountySubmitRequest,
    BountyWithdrawRequest,
    bounty_appeal_decision_confirmation,
    bounty_appeal_message,
    bounty_reason_digest,
    bounty_reassign_confirmation,
    bounty_review_confirmation,
    bounty_slot_confirmation,
    bounty_submit_message,
    bounty_withdraw_message,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_claims import claim_view, locked_bounty
from ditto.api_server.bounty_deadlines import apply_deadlines, settle_deadlines
from ditto.api_server.bounty_settlement import (
    RETAINED_OUTCOMES,
    record_fee_disposition,
)
from ditto.api_server.contributor_identity import (
    ContributorProofRejected,
    check_contributor_proof,
)
from ditto.db.integrity import unique_violation
from ditto.db.models import (
    Bounty,
    BountyActionNonce,
    BountyAppeal,
    BountyAppealDecision,
    BountyClaim,
    BountyEvent,
    BountyReview,
    BountyReviewerAssignment,
    BountyRevision,
    BountySlot,
    BountySlotAction,
    BountySubmission,
    BountyWithdrawal,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_models.contributor_identity import ContributorProof

# Slot statuses a contributor can still deliver from.
DELIVERING = ("claimed", "rework")
APPEALABLE = {"rejected": "rejected", "deadline_missed": "expired"}
CANCELLABLE = ("open", "claimed", "submitted", "rework")
REOPENABLE = ("expired", "withdrawn", "rejected", "cancelled")


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or bounty_claims.utcnow)()


async def _settle_for_claim(
    maker: async_sessionmaker[AsyncSession], claim_id: UUID, *, now: datetime
) -> None:
    async with maker() as session:
        bounty_id = await session.scalar(
            select(BountyClaim.bounty_id).where(BountyClaim.claim_id == claim_id)
        )
    if bounty_id is None:
        raise HTTPException(404, "bounty claim not found")
    await settle_deadlines(maker, bounty_id, now=now)


async def _settle_for_slot(
    maker: async_sessionmaker[AsyncSession], slot_id: UUID, *, now: datetime
) -> None:
    async with maker() as session:
        bounty_id = await session.scalar(
            select(BountySlot.bounty_id).where(BountySlot.slot_id == slot_id)
        )
    if bounty_id is None:
        raise HTTPException(404, "bounty slot not found")
    await settle_deadlines(maker, bounty_id, now=now)


def _check_actor(actor: str, name: str) -> str:
    value = actor.strip()
    if not value or len(value) > 120 or not value.isprintable():
        raise HTTPException(400, f"{name} must be at most 120 printable characters")
    return value


def _check_reason(reason: str) -> None:
    if not reason.strip():
        raise HTTPException(400, "reason must not be blank")


def _verify_signed(
    *,
    claim: BountyClaim,
    netuid: int,
    body_netuid: int,
    hotkey: str,
    proof: ContributorProof,
    payload: bytes,
    issued_at: datetime,
    now: datetime,
) -> None:
    """Only the claimant acts on a claim: its hotkey or its payee of record."""
    if body_netuid != netuid:
        raise HTTPException(400, "netuid does not match this deployment")
    if hotkey != claim.hotkey:
        raise HTTPException(403, "only the claimant's hotkey may act on this claim")
    if proof.key_kind == "coldkey" and proof.signer != claim.payee_coldkey:
        raise HTTPException(
            403, "a coldkey proof must be signed by the claim's payee of record"
        )
    try:
        check_contributor_proof(
            payload=payload,
            hotkey=hotkey,
            key_kind=proof.key_kind,
            signer=proof.signer,
            signature=proof.signature,
            issued_at=issued_at,
            now=now,
        )
    except ContributorProofRejected as exc:
        raise HTTPException(400, str(exc)) from exc


async def _claim(session: AsyncSession, claim_id: UUID) -> BountyClaim:
    claim = await session.get(BountyClaim, claim_id)
    if claim is None:
        raise HTTPException(404, "bounty claim not found")
    return claim


async def _lock_slot(session: AsyncSession, slot_id: UUID) -> BountySlot:
    slot = await session.scalar(
        select(BountySlot)
        .where(BountySlot.slot_id == slot_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert slot is not None
    return slot


async def _slot_of(session: AsyncSession, claim: BountyClaim) -> BountySlot:
    return await _lock_slot(session, claim.slot_id)


def _event(
    session: AsyncSession,
    slot: BountySlot,
    claim: BountyClaim | None,
    kind: str,
    at: datetime,
    **refs: UUID,
) -> None:
    session.add(
        BountyEvent(
            bounty_id=slot.bounty_id,
            kind=kind,
            revision=claim.revision if claim else None,
            round=slot.round,
            slot_id=slot.slot_id,
            claim_id=claim.claim_id if claim else None,
            recorded_at=at,
            **refs,
        )
    )


async def _revision_of(session: AsyncSession, claim: BountyClaim) -> BountyRevision:
    revision = await session.get(BountyRevision, (claim.bounty_id, claim.revision))
    assert revision is not None
    return revision


async def _nonce_retry(
    maker: async_sessionmaker[AsyncSession],
    *,
    nonce: UUID,
    model: type[BountySubmission] | type[BountyWithdrawal] | type[BountyAppeal],
    same: Callable[[object], bool],
    claim_id: UUID,
    signed: tuple[str, int],
    netuid: int,
    repo: str | None = None,
    now: datetime,
) -> BountyClaimView | None:
    """The original outcome for an exact retry; 409 for any other nonce reuse.

    An exact retry repeats every signed field. The ones the record does not
    keep (hotkey, netuid and, for a submission, the repo) must match the claim
    and deployment the original was verified against.
    """
    async with maker() as session:
        if await session.get(BountyActionNonce, nonce) is None:
            return None
        record = await session.scalar(select(model).where(model.nonce == nonce))
        if record is None or not same(record):
            raise HTTPException(409, "bounty action nonce already used")
        claim = await _claim(session, claim_id)
        bounty = await session.get(Bounty, claim.bounty_id)
        assert bounty is not None
        if signed != (claim.hotkey, netuid) or repo not in (None, bounty.repo):
            raise HTTPException(409, "bounty action nonce already used")
        return await claim_view(session, claim, now=now)


def _proof_fields(record: object) -> tuple[object, ...]:
    return (
        record.issued_at,  # type: ignore[attr-defined]
        record.key_kind,  # type: ignore[attr-defined]
        record.signer,  # type: ignore[attr-defined]
        record.signature.lower(),  # type: ignore[attr-defined]
    )


def _body_proof(issued_at: datetime, proof: ContributorProof) -> tuple[object, ...]:
    return (issued_at, proof.key_kind, proof.signer, proof.signature.lower())


async def _signed_action(
    maker: async_sessionmaker[AsyncSession],
    *,
    run: Callable[[AsyncSession], object],
    retry: Callable[[], object],
) -> BountyClaimView:
    """Run a signed action; a concurrent exact retry returns the original."""
    try:
        async with maker() as session, session.begin():
            return await run(session)  # type: ignore[misc]
    except IntegrityError as exc:
        if unique_violation(exc) != "pk_bounty_action_nonces":
            raise
        result = await retry()  # type: ignore[misc]
        if result is None:
            raise
        return result  # type: ignore[return-value]


async def submit_work(
    maker: async_sessionmaker[AsyncSession],
    *,
    body: BountySubmitRequest,
    netuid: int,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Record the signed exact commit and start its review window."""
    now = _now(clock)

    def same(record: object) -> bool:
        return (
            record.claim_id,  # type: ignore[attr-defined]
            record.pr_number,  # type: ignore[attr-defined]
            record.head_sha,  # type: ignore[attr-defined]
            *_proof_fields(record),
        ) == (
            body.claim_id,
            body.pr_number,
            body.head_sha,
            *_body_proof(body.issued_at, body.proof),
        )

    async def retry() -> BountyClaimView | None:
        return await _nonce_retry(
            maker,
            nonce=body.nonce,
            model=BountySubmission,
            same=same,
            claim_id=body.claim_id,
            signed=(body.hotkey, body.netuid),
            netuid=netuid,
            repo=body.repo,
            now=now,
        )

    if (original := await retry()) is not None:
        return original
    await _settle_for_claim(maker, body.claim_id, now=now)

    async def run(session: AsyncSession) -> BountyClaimView:
        claim = await _claim(session, body.claim_id)
        _verify_signed(
            claim=claim,
            netuid=netuid,
            body_netuid=body.netuid,
            hotkey=body.hotkey,
            proof=body.proof,
            payload=bounty_submit_message(
                netuid=body.netuid,
                claim_id=body.claim_id,
                hotkey=body.hotkey,
                repo=body.repo,
                pr_number=body.pr_number,
                head_sha=body.head_sha,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            issued_at=body.issued_at,
            now=now,
        )
        bounty = await locked_bounty(session, claim.bounty_id)
        if body.repo != bounty.repo:
            raise HTTPException(
                400, f"the pull request must be in the bounty repository {bounty.repo}"
            )
        await apply_deadlines(session, claim.bounty_id, now=now)
        slot = await _slot_of(session, claim)
        if slot.claim_id != claim.claim_id or slot.status not in DELIVERING:
            raise HTTPException(
                409, f"this claim cannot submit work: its slot is {slot.status}"
            )
        revision = await _revision_of(session, claim)
        session.add(
            BountyActionNonce(nonce=body.nonce, action="submit", recorded_at=now)
        )
        await session.flush()
        submission = BountySubmission(
            submission_id=uuid4(),
            claim_id=claim.claim_id,
            pr_number=body.pr_number,
            head_sha=body.head_sha,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
            signature=body.proof.signature,
            submitted_at=now,
            review_due_at=now + timedelta(days=revision.review_days),
        )
        session.add(submission)
        await session.flush()
        slot.status, slot.status_changed_at = "submitted", now
        slot.review_due_at = submission.review_due_at
        _event(
            session,
            slot,
            claim,
            "submitted",
            now,
            submission_id=submission.submission_id,
        )
        await session.flush()
        return await claim_view(session, claim, now=now)

    return await _signed_action(maker, run=run, retry=retry)


async def withdraw_claim(
    maker: async_sessionmaker[AsyncSession],
    *,
    body: BountyWithdrawRequest,
    netuid: int,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Give up a claimed or rework slot. The entry fee is retained."""
    now = _now(clock)

    def same(record: object) -> bool:
        return (record.claim_id, *_proof_fields(record)) == (  # type: ignore[attr-defined]
            body.claim_id,
            *_body_proof(body.issued_at, body.proof),
        )

    async def retry() -> BountyClaimView | None:
        return await _nonce_retry(
            maker,
            nonce=body.nonce,
            model=BountyWithdrawal,
            same=same,
            claim_id=body.claim_id,
            signed=(body.hotkey, body.netuid),
            netuid=netuid,
            now=now,
        )

    if (original := await retry()) is not None:
        return original
    await _settle_for_claim(maker, body.claim_id, now=now)

    async def run(session: AsyncSession) -> BountyClaimView:
        claim = await _claim(session, body.claim_id)
        _verify_signed(
            claim=claim,
            netuid=netuid,
            body_netuid=body.netuid,
            hotkey=body.hotkey,
            proof=body.proof,
            payload=bounty_withdraw_message(
                netuid=body.netuid,
                claim_id=body.claim_id,
                hotkey=body.hotkey,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            issued_at=body.issued_at,
            now=now,
        )
        await locked_bounty(session, claim.bounty_id)
        await apply_deadlines(session, claim.bounty_id, now=now)
        slot = await _slot_of(session, claim)
        if slot.claim_id != claim.claim_id or slot.status not in DELIVERING:
            raise HTTPException(
                409, f"this claim cannot be withdrawn: its slot is {slot.status}"
            )
        session.add(
            BountyActionNonce(nonce=body.nonce, action="withdraw", recorded_at=now)
        )
        await session.flush()
        session.add(
            BountyWithdrawal(
                claim_id=claim.claim_id,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
                signature=body.proof.signature,
                withdrawn_at=now,
            )
        )
        slot.status, slot.status_changed_at = "withdrawn", now
        _event(session, slot, claim, "withdrawn", now)
        await session.flush()
        return await claim_view(session, claim, now=now)

    return await _signed_action(maker, run=run, retry=retry)


async def current_reviewer(session: AsyncSession, bounty_id: UUID) -> str:
    """The reviewer in force: the latest of publication and reassignment."""
    revision = await session.scalar(
        select(BountyRevision)
        .where(BountyRevision.bounty_id == bounty_id)
        .order_by(BountyRevision.revision.desc())
        .limit(1)
    )
    assert revision is not None
    # assignment_id is a BIGINT IDENTITY on an append-only table, so the
    # highest is the latest assignment, even when two share an assigned_at.
    assignment = await session.scalar(
        select(BountyReviewerAssignment)
        .where(BountyReviewerAssignment.bounty_id == bounty_id)
        .order_by(BountyReviewerAssignment.assignment_id.desc())
        .limit(1)
    )
    if assignment is not None and assignment.assigned_at >= revision.published_at:
        return assignment.reviewer_actor
    return revision.reviewer_actor


async def review_submission(
    maker: async_sessionmaker[AsyncSession],
    *,
    claim_id: UUID,
    body: BountyReviewRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """The assigned reviewer's decision on one exact submission."""
    _check_reason(body.reason)
    if (body.decision == "changes_requested") != (body.rework_days is not None):
        raise HTTPException(
            400, "rework_days is required to request changes, and only then"
        )
    if body.attribution is not None and body.decision != "accept":
        raise HTTPException(400, "attribution is recorded only for accepted work")
    if claim_id != body.claim_id:
        raise HTTPException(400, "claim_id does not match the route")
    now = _now(clock)
    await _settle_for_claim(maker, claim_id, now=now)
    async with maker() as session, session.begin():
        claim = await _claim(session, claim_id)
        bounty = await locked_bounty(session, claim.bounty_id)
        expected = bounty_review_confirmation(
            repo=bounty.repo,
            issue=bounty.issue_number,
            claim_id=claim.claim_id,
            decision=body.decision,
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        if actor != await current_reviewer(session, claim.bounty_id):
            raise HTTPException(403, "only the bounty's assigned reviewer may decide")
        decided = await session.get(BountyReview, body.submission_id)
        if decided is not None:
            # An exact retry repeats every recorded field; the rework deadline
            # it implies is counted from the original decision.
            retry_deadline = (
                decided.decided_at + timedelta(days=body.rework_days)
                if body.rework_days is not None
                else None
            )
            if (
                decided.claim_id,
                decided.reviewer_actor,
                decided.decision,
                decided.reason,
                decided.attribution,
                decided.rework_deadline_at,
            ) == (
                claim.claim_id,
                actor,
                body.decision,
                body.reason,
                body.attribution,
                retry_deadline,
            ):
                return await claim_view(session, claim, now=now)
            raise HTTPException(409, "this submission was already decided")
        await apply_deadlines(session, claim.bounty_id, now=now)
        slot = await _slot_of(session, claim)
        latest = await session.scalar(
            select(BountySubmission)
            .where(BountySubmission.claim_id == claim.claim_id)
            .order_by(BountySubmission.submitted_at.desc())
            .limit(1)
        )
        if (
            slot.claim_id != claim.claim_id
            or slot.status != "submitted"
            or latest is None
            or latest.submission_id != body.submission_id
        ):
            raise HTTPException(
                409,
                "that submission is no longer under review; read the claim again",
            )
        rework_deadline = (
            now + timedelta(days=body.rework_days)
            if body.rework_days is not None
            else None
        )
        session.add(
            BountyReview(
                submission_id=latest.submission_id,
                claim_id=claim.claim_id,
                decision=body.decision,
                reason=body.reason,
                attribution=body.attribution,
                rework_deadline_at=rework_deadline,
                reviewer_actor=actor,
                decided_at=now,
            )
        )
        await session.flush()
        slot.status_changed_at, slot.review_due_at = now, None
        if body.decision == "accept":
            slot.status, kind = "accepted", "accepted"
            # Accepted work has its entry fee refunded in full.
            await record_fee_disposition(
                session,
                claim=claim,
                slot=slot,
                kind="fee_refund",
                reason="accepted",
                now=now,
            )
        elif body.decision == "reject":
            slot.status, kind = "rejected", "rejected"
        else:
            slot.status, kind = "rework", "rework_requested"
            slot.deadline_at = rework_deadline
        _event(session, slot, claim, kind, now, submission_id=latest.submission_id)
        await session.flush()
        return await claim_view(session, claim, now=now)


async def reassign_reviewer(
    maker: async_sessionmaker[AsyncSession],
    *,
    bounty_id: UUID,
    body: BountyReassignRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Name a new reviewer; overdue reviews get one more review window."""
    _check_reason(body.reason)
    reviewer = _check_actor(body.reviewer_actor, "reviewer_actor")
    now = _now(clock)
    await settle_deadlines(maker, bounty_id, now=now)
    async with maker() as session, session.begin():
        bounty = await locked_bounty(session, bounty_id)
        expected = bounty_reassign_confirmation(
            repo=bounty.repo, issue=bounty.issue_number
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        if reviewer == await current_reviewer(session, bounty_id):
            raise HTTPException(409, "that operator is already the reviewer")
        await apply_deadlines(session, bounty_id, now=now)
        session.add(
            BountyReviewerAssignment(
                bounty_id=bounty_id,
                reviewer_actor=reviewer,
                actor=actor,
                reason=body.reason,
                assigned_at=now,
            )
        )
        rows = (
            await session.execute(
                select(BountySlot, BountyClaim)
                .join(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
                .where(
                    BountySlot.bounty_id == bounty_id,
                    BountySlot.status == "submitted",
                    BountySlot.review_due_at <= now,
                )
                .with_for_update(of=BountySlot)
            )
        ).all()
        for slot, claim in rows:
            revision = await _revision_of(session, claim)
            slot.review_due_at = now + timedelta(days=revision.review_days)
        session.add(
            BountyEvent(
                bounty_id=bounty_id, kind="reviewer_reassigned", recorded_at=now
            )
        )


async def file_appeal(
    maker: async_sessionmaker[AsyncSession],
    *,
    body: BountyAppealRequest,
    netuid: int,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Appeal one rejection or missed deadline of the claimant's own claim."""
    _check_reason(body.reason)
    now = _now(clock)
    digest = bounty_reason_digest(body.reason)

    def same(record: object) -> bool:
        return (
            record.claim_id,  # type: ignore[attr-defined]
            record.subject_event_id,  # type: ignore[attr-defined]
            record.reason_digest,  # type: ignore[attr-defined]
            *_proof_fields(record),
        ) == (
            body.claim_id,
            body.subject_event_id,
            digest,
            *_body_proof(body.issued_at, body.proof),
        )

    async def retry() -> BountyClaimView | None:
        return await _nonce_retry(
            maker,
            nonce=body.nonce,
            model=BountyAppeal,
            same=same,
            claim_id=body.claim_id,
            signed=(body.hotkey, body.netuid),
            netuid=netuid,
            now=now,
        )

    if (original := await retry()) is not None:
        return original
    await _settle_for_claim(maker, body.claim_id, now=now)

    async def run(session: AsyncSession) -> BountyClaimView:
        claim = await _claim(session, body.claim_id)
        _verify_signed(
            claim=claim,
            netuid=netuid,
            body_netuid=body.netuid,
            hotkey=body.hotkey,
            proof=body.proof,
            payload=bounty_appeal_message(
                netuid=body.netuid,
                claim_id=body.claim_id,
                hotkey=body.hotkey,
                subject_event_id=body.subject_event_id,
                reason_digest=digest,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=body.proof.key_kind,
                signer=body.proof.signer,
            ),
            issued_at=body.issued_at,
            now=now,
        )
        await locked_bounty(session, claim.bounty_id)
        await apply_deadlines(session, claim.bounty_id, now=now)
        subject = await session.get(BountyEvent, body.subject_event_id)
        if (
            subject is None
            or subject.claim_id != claim.claim_id
            or subject.kind not in APPEALABLE
        ):
            raise HTTPException(
                409, "only this claim's rejection or missed deadline can be appealed"
            )
        if await session.scalar(
            select(BountyAppeal.appeal_id).where(
                BountyAppeal.subject_event_id == subject.event_id
            )
        ):
            raise HTTPException(409, "this decision was already appealed")
        slot = await _slot_of(session, claim)
        if slot.claim_id != claim.claim_id or slot.status != APPEALABLE[subject.kind]:
            raise HTTPException(409, "the slot has moved on; nothing to appeal")
        session.add(
            BountyActionNonce(nonce=body.nonce, action="appeal", recorded_at=now)
        )
        await session.flush()
        appeal = BountyAppeal(
            appeal_id=uuid4(),
            claim_id=claim.claim_id,
            subject_event_id=subject.event_id,
            reason=body.reason,
            reason_digest=digest,
            nonce=body.nonce,
            issued_at=body.issued_at,
            key_kind=body.proof.key_kind,
            signer=body.proof.signer,
            signature=body.proof.signature,
            filed_at=now,
        )
        session.add(appeal)
        await session.flush()
        _event(session, slot, claim, "appeal_filed", now, appeal_id=appeal.appeal_id)
        await session.flush()
        return await claim_view(session, claim, now=now)

    return await _signed_action(maker, run=run, retry=retry)


async def decide_appeal(
    maker: async_sessionmaker[AsyncSession],
    *,
    appeal_id: UUID,
    body: BountyAppealDecisionRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Uphold or deny an appeal; upholding restores the claim to deliver again."""
    _check_reason(body.reason)
    if body.upheld != (body.restore_days is not None):
        raise HTTPException(
            400, "restore_days is required to uphold an appeal, and only then"
        )
    now = _now(clock)
    async with maker() as session:
        appealed = await session.scalar(
            select(BountyAppeal.claim_id).where(BountyAppeal.appeal_id == appeal_id)
        )
    if appealed is None:
        raise HTTPException(404, "bounty appeal not found")
    await _settle_for_claim(maker, appealed, now=now)
    async with maker() as session, session.begin():
        appeal = await session.get(BountyAppeal, appeal_id)
        if appeal is None:
            raise HTTPException(404, "bounty appeal not found")
        claim = await _claim(session, appeal.claim_id)
        await locked_bounty(session, claim.bounty_id)
        expected = bounty_appeal_decision_confirmation(
            appeal_id=appeal_id, upheld=body.upheld
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        decided = await session.get(BountyAppealDecision, appeal_id)
        if decided is not None:
            # An exact retry is the same operator's same decision; the deadline
            # it restores is counted from the original decision.
            restores = (
                decided.decided_at + timedelta(days=body.restore_days)
                if body.restore_days is not None
                else None
            )
            if (
                decided.actor,
                decided.upheld,
                decided.reason,
                decided.restored_deadline_at,
            ) == (actor, body.upheld, body.reason, restores):
                return await claim_view(session, claim, now=now)
            raise HTTPException(409, "this appeal was already decided")
        subject = await session.get(BountyEvent, appeal.subject_event_id)
        assert subject is not None
        if subject.kind == "rejected":
            review = await session.get(BountyReview, subject.submission_id)
            # Without the review, the reviewer exclusion cannot be checked.
            if review is None:
                raise HTTPException(
                    409, "the rejected submission has no recorded review"
                )
            if review.reviewer_actor == actor:
                raise HTTPException(
                    403, "an appeal is decided by someone other than its reviewer"
                )
        elif subject.kind == "deadline_missed":
            # A missed rework deadline was set by the reviewer who asked for
            # changes; it is recorded at that deadline. A first delivery
            # deadline has no reviewer to exclude.
            setter = await session.scalar(
                select(BountyReview.reviewer_actor).where(
                    BountyReview.claim_id == claim.claim_id,
                    BountyReview.decision == "changes_requested",
                    BountyReview.rework_deadline_at == subject.recorded_at,
                )
            )
            if setter == actor:
                raise HTTPException(
                    403, "an appeal is decided by someone other than its reviewer"
                )
        await apply_deadlines(session, claim.bounty_id, now=now)
        slot = await _slot_of(session, claim)
        if slot.claim_id != claim.claim_id or slot.status != APPEALABLE[subject.kind]:
            raise HTTPException(409, "the slot has moved on; the appeal cannot apply")
        restores = (
            now + timedelta(days=body.restore_days)
            if body.restore_days is not None
            else None
        )
        session.add(
            BountyAppealDecision(
                appeal_id=appeal_id,
                upheld=body.upheld,
                reason=body.reason,
                actor=actor,
                decided_at=now,
                restored_deadline_at=restores,
            )
        )
        await session.flush()
        if restores is not None:
            slot.status, slot.status_changed_at = "claimed", now
            slot.deadline_at = restores
            slot.review_due_at = None
        kind = "appeal_upheld" if body.upheld else "appeal_denied"
        _event(session, slot, claim, kind, now, appeal_id=appeal_id)
        await session.flush()
        return await claim_view(session, claim, now=now)


async def cancel_slot(
    maker: async_sessionmaker[AsyncSession],
    *,
    slot_id: UUID,
    body: BountyCancelRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    """Cancel a slot; a paid entry fee is refunded in full."""
    _check_reason(body.reason)
    now = _now(clock)
    await _settle_for_slot(maker, slot_id, now=now)
    async with maker() as session, session.begin():
        slot = await session.get(BountySlot, slot_id)
        if slot is None:
            raise HTTPException(404, "bounty slot not found")
        bounty = await locked_bounty(session, slot.bounty_id)
        expected = bounty_slot_confirmation(
            action="cancel",
            repo=bounty.repo,
            issue=bounty.issue_number,
            round_number=slot.round,
            position=slot.position,
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        await apply_deadlines(session, slot.bounty_id, now=now)
        slot = await _lock_slot(session, slot_id)
        if slot.status not in CANCELLABLE:
            raise HTTPException(409, f"a {slot.status} slot cannot be cancelled")
        claim = await session.get(BountyClaim, slot.claim_id) if slot.claim_id else None
        session.add(
            BountySlotAction(
                slot_id=slot.slot_id,
                claim_id=slot.claim_id,
                action="cancel",
                cancel_kind=body.kind,
                prior_status=slot.status,
                actor=actor,
                reason=body.reason,
                recorded_at=now,
            )
        )
        slot.status, slot.status_changed_at = "cancelled", now
        _event(session, slot, claim, "slot_cancelled", now)
        if claim is not None:
            # Claimed and later statuses were paid: refund that fee in full.
            refund = await record_fee_disposition(
                session,
                claim=claim,
                slot=slot,
                kind="fee_refund",
                reason="cancelled" if body.kind == "team" else "system_fault",
                now=now,
            )
            assert refund is not None
        return slot.bounty_id


async def reopen_slot(
    maker: async_sessionmaker[AsyncSession],
    *,
    slot_id: UUID,
    body: BountyReopenRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> UUID:
    """Reopen a closed slot for a new claim. Never automatic."""
    _check_reason(body.reason)
    now = _now(clock)
    await _settle_for_slot(maker, slot_id, now=now)
    async with maker() as session, session.begin():
        slot = await session.get(BountySlot, slot_id)
        if slot is None:
            raise HTTPException(404, "bounty slot not found")
        bounty = await locked_bounty(session, slot.bounty_id)
        expected = bounty_slot_confirmation(
            action="reopen",
            repo=bounty.repo,
            issue=bounty.issue_number,
            round_number=slot.round,
            position=slot.position,
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        await apply_deadlines(session, slot.bounty_id, now=now)
        slot = await _lock_slot(session, slot_id)
        if slot.status not in REOPENABLE:
            raise HTTPException(409, f"a {slot.status} slot cannot be reopened")
        claim = await session.get(BountyClaim, slot.claim_id) if slot.claim_id else None
        if claim is not None and await session.scalar(
            select(BountyAppeal.appeal_id)
            .outerjoin(
                BountyAppealDecision,
                BountyAppealDecision.appeal_id == BountyAppeal.appeal_id,
            )
            .where(
                BountyAppeal.claim_id == claim.claim_id,
                BountyAppealDecision.appeal_id.is_(None),
            )
        ):
            raise HTTPException(409, "decide the claim's pending appeal first")
        session.add(
            BountySlotAction(
                slot_id=slot.slot_id,
                claim_id=slot.claim_id,
                action="reopen",
                prior_status=slot.status,
                actor=actor,
                reason=body.reason,
                recorded_at=now,
            )
        )
        if claim is not None and slot.status in RETAINED_OUTCOMES:
            # Reopening makes the outcome final: the fee is retained.
            await record_fee_disposition(
                session,
                claim=claim,
                slot=slot,
                kind="fee_retained",
                reason=slot.status,
                now=now,
            )
        _event(session, slot, claim, "slot_reopened", now)
        slot.status, slot.claim_id, slot.status_changed_at = "open", None, now
        slot.deadline_at = slot.review_due_at = None
        return slot.bounty_id


async def bounty_of_claim(session: AsyncSession, claim_id: UUID) -> Bounty:
    claim = await _claim(session, claim_id)
    bounty = await session.get(Bounty, claim.bounty_id)
    assert bounty is not None
    return bounty
