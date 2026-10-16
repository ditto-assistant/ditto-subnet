"""Paid bounty claims: signed holds, fee confirmation and refund-on-loss.

A contributor signs a claim for the current bounty revision. Platform checks
the signature, the finalized chain owner (the payee of record), the verified
GitHub link and a current CLA signature, then holds the lowest open slot of
the current round for ``hold_minutes``. A verified fee receipt then turns
the hold into a claimed slot whose delivery deadline starts at that moment.

No payment is ever forfeited by timing or a race: a receipt that cannot win
its slot (a lapsed hold, a lost slot, a changed identity, a second payment)
is recorded with a full fee-refund obligation. A receipt is charged at most
once, and an exact retry returns the original outcome.

In v1 every bounty is in rehearsal funding, and the only fee verifier is the
simulated one: each claim has exactly one rehearsal receipt, derived from its
id, and no funds move. Live verification plugs in behind the same interface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError

from ditto.api_models.bounty import (
    BountyAppealView,
    BountyClaimRequest,
    BountyClaimsPauseRequest,
    BountyClaimView,
    BountyConfirmRequest,
    BountyFactView,
    BountyFeeReceipt,
    BountyRefundView,
    BountySubmissionView,
    bounty_claim_message,
    bounty_pause_confirmation,
    bounty_team_digest,
    simulated_fee_block_hash,
)
from ditto.api_server.bounty_deadlines import (
    DEADLINE_STATUSES,
    apply_deadlines,
    effective_slot_status,
    release_slot,
    settle_deadlines,
)
from ditto.api_server.bounty_identity import (
    BountyIdentityRejected,
    BountyIdentityUnavailable,
    prepare_bounty_identity,
    require_verified_bounty_contributor,
)
from ditto.api_server.bounty_obligations import current_policy, obligation_view
from ditto.api_server.cla import ClaConsentRequired
from ditto.api_server.contributor_identity import (
    ContributorProofRejected,
    check_contributor_proof,
    finalized_owner,
)
from ditto.chain import ChainError
from ditto.db.integrity import unique_violation
from ditto.db.models import (
    Bounty,
    BountyActionNonce,
    BountyAppeal,
    BountyAppealDecision,
    BountyClaim,
    BountyClaimControl,
    BountyDeliveryFact,
    BountyEvent,
    BountyFeePayment,
    BountyObligation,
    BountyReview,
    BountyRevision,
    BountyRound,
    BountySlot,
    BountySlotAction,
    BountySubmission,
)
from ditto.db.queries.contributor_identity import (
    active_github_link,
    current_cla_signature,
    current_cla_version,
    lock_identity_writes,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_server.github_link import GithubLinkClient
    from ditto.chain import ChainClient

# Slots a live claim occupies. A held slot counts only while its hold lasts.
LIVE_SLOT_STATUSES = ("held", "claimed", "submitted", "rework")


class BountyFeeRejected(Exception):
    """The receipt does not pay this claim's fee; nothing is recorded."""


@dataclass(frozen=True)
class VerifiedBountyFee:
    block_hash: str
    block_number: int
    extrinsic_index: int
    payer_coldkey: str
    amount_alpha_rao: int
    simulated: bool


class BountyFeeVerifier(Protocol):
    async def verify(
        self, receipt: BountyFeeReceipt, *, claim: BountyClaim
    ) -> VerifiedBountyFee: ...


class SimulatedBountyFeeVerifier:
    """Rehearsal fees: one receipt per claim, derived from its id. No funds."""

    async def verify(
        self, receipt: BountyFeeReceipt, *, claim: BountyClaim
    ) -> VerifiedBountyFee:
        if claim.funding_mode != "rehearsal":
            raise BountyFeeRejected(
                "live bounty fees need the live fee verifier, which is not enabled"
            )
        if (receipt.block_hash, receipt.block_number, receipt.extrinsic_index) != (
            simulated_fee_block_hash(claim.claim_id),
            0,
            0,
        ):
            raise BountyFeeRejected("this is not the claim's rehearsal fee receipt")
        return VerifiedBountyFee(
            block_hash=receipt.block_hash,
            block_number=receipt.block_number,
            extrinsic_index=receipt.extrinsic_index,
            payer_coldkey=claim.payee_coldkey,
            amount_alpha_rao=claim.fee_alpha_rao,
            simulated=True,
        )


def utcnow() -> datetime:
    """The claim engine's and the public read model's one clock."""
    return datetime.now(UTC)


async def locked_bounty(session: AsyncSession, bounty_id: UUID) -> Bounty:
    # One row lock per bounty serializes holds, confirmations and pauses.
    bounty = await session.scalar(
        select(Bounty).where(Bounty.bounty_id == bounty_id).with_for_update()
    )
    if bounty is None:
        raise HTTPException(404, "bounty not found")
    return bounty


async def _current_revision(session: AsyncSession, bounty_id: UUID) -> BountyRevision:
    row = await session.scalar(
        select(BountyRevision)
        .where(BountyRevision.bounty_id == bounty_id)
        .order_by(BountyRevision.revision.desc())
        .limit(1)
    )
    assert row is not None  # A bounty is created with its first revision.
    return row


async def claims_paused(session: AsyncSession, bounty_id: UUID) -> bool:
    paused = await session.scalar(
        select(BountyClaimControl.paused)
        .where(BountyClaimControl.bounty_id == bounty_id)
        .order_by(BountyClaimControl.control_id.desc())
        .limit(1)
    )
    return bool(paused)


def _same_claim(claim: BountyClaim, body: BountyClaimRequest) -> bool:
    return (
        claim.netuid,
        claim.revision,
        claim.spec_digest,
        claim.hotkey,
        claim.payee_coldkey,
        claim.github_login,
        claim.team_digest,
        claim.key_kind,
        claim.signer,
        claim.signature.lower(),
    ) == (
        body.netuid,
        body.revision,
        body.spec_digest,
        body.hotkey,
        body.payee_coldkey,
        body.github_login,
        body.team_digest,
        body.proof.key_kind,
        body.proof.signer,
        body.proof.signature.lower(),
    ) and claim.issued_at == body.issued_at


async def _retry_of(
    session: AsyncSession, body: BountyClaimRequest, *, now: datetime
) -> BountyClaimView | None:
    """The original claim for an exact retry; 409 for any other nonce reuse."""
    if await session.get(BountyActionNonce, body.nonce) is None:
        return None
    claim = await session.scalar(
        select(BountyClaim).where(BountyClaim.nonce == body.nonce)
    )
    if claim is None or not _same_claim(claim, body):
        raise HTTPException(409, "bounty action nonce already used")
    return await claim_view(session, claim, now=now)


async def hold_claim(
    maker: async_sessionmaker[AsyncSession],
    *,
    chain: ChainClient,
    github: GithubLinkClient,
    body: BountyClaimRequest,
    netuid: int,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Verify a signed claim and hold one open slot for its fee payment."""
    now = (clock or utcnow)()
    if body.netuid != netuid:
        raise HTTPException(400, "claim netuid does not match this deployment")
    if body.team_digest != bounty_team_digest(
        payee_coldkey=body.payee_coldkey, members=[(body.hotkey, body.github_login)]
    ):
        raise HTTPException(
            400,
            "v1 claims are solo: team_digest must be the one-member team of this "
            "hotkey, login and payee",
        )
    proof = body.proof
    if proof.key_kind == "coldkey" and proof.signer != body.payee_coldkey:
        raise HTTPException(400, "a coldkey proof must be signed by the payee coldkey")
    try:
        check_contributor_proof(
            payload=bounty_claim_message(
                netuid=body.netuid,
                repo=body.repo,
                issue=body.issue,
                revision=body.revision,
                spec_digest=body.spec_digest,
                hotkey=body.hotkey,
                payee_coldkey=body.payee_coldkey,
                github_login=body.github_login,
                team_digest=body.team_digest,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=proof.key_kind,
                signer=proof.signer,
            ),
            hotkey=body.hotkey,
            key_kind=proof.key_kind,
            signer=proof.signer,
            signature=proof.signature,
            issued_at=body.issued_at,
            now=now,
        )
    except ContributorProofRejected as exc:
        raise HTTPException(400, str(exc)) from exc

    # Cheap refusals and exact retries before any chain or GitHub call.
    async with maker() as session:
        bounty_id = await session.scalar(
            select(Bounty.bounty_id).where(
                Bounty.repo == body.repo, Bounty.issue_number == body.issue
            )
        )
        if bounty_id is None:
            raise HTTPException(404, "bounty not found")
        retry = await _retry_of(session, body, now=now)
        if retry is not None:
            return retry
        _check_terms(await _current_revision(session, bounty_id), body)
        if await claims_paused(session, bounty_id):
            raise HTTPException(409, "new claims are paused for this bounty")

    try:
        owner = await finalized_owner(chain, body.hotkey)
    except ContributorProofRejected as exc:
        raise HTTPException(403, str(exc)) from exc
    except ChainError:
        raise HTTPException(503, "finalized ownership unavailable; retry") from None
    if owner.coldkey != body.payee_coldkey:
        raise HTTPException(
            403, "payee_coldkey must be Owner(hotkey) at the finalized block"
        )
    try:
        evidence = await prepare_bounty_identity(
            maker,
            netuid=netuid,
            hotkey=body.hotkey,
            github_login=body.github_login,
            github=github,
        )
    except BountyIdentityUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except BountyIdentityRejected as exc:
        raise HTTPException(403, str(exc)) from exc

    # The hold and every record of it start after verification, so a slow
    # chain or GitHub call can't shorten it or leave a lapsed hold standing.
    now = (clock or utcnow)()
    await settle_deadlines(maker, bounty_id, now=now)
    try:
        async with maker() as session, session.begin():
            await locked_bounty(session, bounty_id)
            # The lock may have waited; the hold starts once it is held.
            now = (clock or utcnow)()
            revision = await _current_revision(session, bounty_id)
            _check_terms(revision, body)
            if await claims_paused(session, bounty_id):
                raise HTTPException(409, "new claims are paused for this bounty")
            try:
                verified = await require_verified_bounty_contributor(
                    session,
                    netuid=netuid,
                    hotkey=body.hotkey,
                    github_login=body.github_login,
                    evidence=evidence,
                )
            except (BountyIdentityRejected, ClaConsentRequired) as exc:
                raise HTTPException(403, str(exc)) from exc
            session.add(
                BountyActionNonce(nonce=body.nonce, action="claim", recorded_at=now)
            )
            await session.flush()
            await apply_deadlines(session, bounty_id, now=now)
            holder = await session.scalar(
                select(BountyClaim.claim_id)
                .join(BountySlot, BountySlot.claim_id == BountyClaim.claim_id)
                .where(
                    BountySlot.bounty_id == bounty_id,
                    BountySlot.status.in_(LIVE_SLOT_STATUSES),
                    (BountyClaim.github_user_id == verified.github_user_id)
                    | (BountyClaim.payee_coldkey == body.payee_coldkey),
                )
                .limit(1)
            )
            if holder is not None:
                raise HTTPException(
                    409,
                    "this GitHub account or payee coldkey already holds a slot on "
                    "this bounty",
                )
            current_round = await session.scalar(
                select(func.max(BountyRound.round)).where(
                    BountyRound.bounty_id == bounty_id
                )
            )
            slot = await session.scalar(
                select(BountySlot)
                .where(
                    BountySlot.bounty_id == bounty_id,
                    BountySlot.round == current_round,
                    BountySlot.status == "open",
                )
                .order_by(BountySlot.position)
                .limit(1)
                .with_for_update()
            )
            if slot is None:
                raise HTTPException(
                    409, f"every slot in round {current_round} is taken"
                )
            claim = BountyClaim(
                claim_id=uuid4(),
                bounty_id=bounty_id,
                revision=revision.revision,
                spec_digest=revision.spec_digest,
                slot_id=slot.slot_id,
                netuid=netuid,
                hotkey=body.hotkey,
                payee_coldkey=body.payee_coldkey,
                # The login exactly as signed, so the stored claim re-verifies
                # from its own fields; GitHub matched it case-insensitively.
                github_login=body.github_login,
                team_digest=body.team_digest,
                nonce=body.nonce,
                issued_at=body.issued_at,
                key_kind=proof.key_kind,
                signer=proof.signer,
                signature=proof.signature,
                owner_block=owner.block,
                owner_block_hash=owner.block_hash,
                github_link_id=verified.github_link_id,
                github_user_id=verified.github_user_id,
                cla_signature_id=verified.cla_signature_id,
                funding_mode=revision.funding_mode,
                fee_alpha_rao=revision.entry_fee_alpha_rao,
                fee_escrow_coldkey=revision.fee_escrow_coldkey,
                fee_escrow_hotkey=revision.fee_escrow_hotkey,
                held_at=now,
                hold_expires_at=now + timedelta(minutes=revision.hold_minutes),
            )
            session.add(claim)
            await session.flush()
            slot.status, slot.claim_id, slot.status_changed_at = (
                "held",
                claim.claim_id,
                now,
            )
            session.add(
                BountyEvent(
                    bounty_id=bounty_id,
                    kind="slot_held",
                    revision=revision.revision,
                    round=slot.round,
                    slot_id=slot.slot_id,
                    claim_id=claim.claim_id,
                    recorded_at=now,
                )
            )
            await session.flush()
            return await claim_view(session, claim, now=now)
    except IntegrityError as exc:
        # A concurrent request with this nonce committed first.
        if unique_violation(exc) != "pk_bounty_action_nonces":
            raise
        async with maker() as session:
            retry = await _retry_of(session, body, now=now)
        if retry is None:
            raise
        return retry


def _check_terms(revision: BountyRevision, body: BountyClaimRequest) -> None:
    if (revision.revision, revision.spec_digest) != (body.revision, body.spec_digest):
        raise HTTPException(
            409,
            f"bounty terms changed: review revision {revision.revision} "
            f"({revision.spec_digest}) and sign a claim for it",
        )
    if revision.funding_mode != "rehearsal":
        raise HTTPException(409, "live bounty funding is not enabled")


async def _identity_changed(
    session: AsyncSession, claim: BountyClaim, *, owner_coldkey: str | None
) -> bool:
    """Whether the payee, link or CLA signature behind the hold has changed."""
    if owner_coldkey != claim.payee_coldkey:
        return True
    # The hold's own order (bounty, then link, then CLA policy), so an unlink,
    # revocation or new CLA version can't commit between these reads and the
    # fee's allocation. Readers share; identity writers take them exclusively.
    await lock_identity_writes(session, namespace="github-link", shared=True)
    link = await active_github_link(session, hotkey=claim.hotkey)
    if link is None or link.link_id != claim.github_link_id:
        return True
    await lock_identity_writes(session, namespace="cla-policy", shared=True)
    version = await current_cla_version(session)
    signature = await current_cla_signature(
        session, netuid=claim.netuid, hotkey=claim.hotkey, version=version
    )
    # A signature of a newer agreement is consent the hold was not made under.
    return signature is None or signature.signature_id != claim.cla_signature_id


async def confirm_claim(
    maker: async_sessionmaker[AsyncSession],
    *,
    chain: ChainClient,
    verifier: BountyFeeVerifier,
    claim_id: UUID,
    body: BountyConfirmRequest,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Record a verified fee: claim the held slot, or owe a full refund."""
    async with maker() as session:
        claim = await session.get(BountyClaim, claim_id)
        if claim is None:
            raise HTTPException(404, "bounty claim not found")
        session.expunge(claim)
    try:
        fee = await verifier.verify(body.receipt, claim=claim)
    except BountyFeeRejected as exc:
        raise HTTPException(402, str(exc)) from exc
    if fee.payer_coldkey != claim.payee_coldkey or (
        fee.amount_alpha_rao != claim.fee_alpha_rao
    ):
        raise HTTPException(402, "the receipt does not pay this claim's exact fee")
    try:
        owner_coldkey: str | None = (await finalized_owner(chain, claim.hotkey)).coldkey
    except ContributorProofRejected:
        owner_coldkey = None
    except ChainError:
        raise HTTPException(503, "finalized ownership unavailable; retry") from None
    await settle_deadlines(maker, claim.bounty_id, now=(clock or utcnow)())
    async with maker() as session, session.begin():
        await locked_bounty(session, claim.bounty_id)
        # Read after the lock: a wait that crosses the hold's end must lapse
        # it, and a delivery deadline starts when the fee is recorded.
        now = (clock or utcnow)()
        await apply_deadlines(session, claim.bounty_id, now=now)
        claim = await session.get(BountyClaim, claim_id)
        assert claim is not None
        recorded = await session.get(
            BountyFeePayment, (fee.block_hash, fee.extrinsic_index)
        )
        if recorded is not None:
            if recorded.claim_id != claim.claim_id:
                raise HTTPException(409, "this fee receipt already paid another claim")
            return await claim_view(session, claim, now=now)
        slot = await session.scalar(
            select(BountySlot)
            .where(BountySlot.slot_id == claim.slot_id)
            .with_for_update()
        )
        assert slot is not None
        holds_slot = slot.claim_id == claim.claim_id and slot.status == "held"
        reason: str | None = None
        if await session.scalar(
            select(
                exists().where(
                    BountyFeePayment.claim_id == claim.claim_id,
                    BountyFeePayment.outcome == "allocated",
                )
            )
        ):
            reason = "duplicate_payment"
        elif now >= claim.hold_expires_at:
            reason = "hold_expired"
            if holds_slot:
                release_slot(
                    session, slot, claim, kind="hold_expired", at=claim.hold_expires_at
                )
        elif not holds_slot:
            reason = "slot_lost"
        elif await _identity_changed(session, claim, owner_coldkey=owner_coldkey):
            reason = "identity_changed"
            release_slot(session, slot, claim, kind="hold_released", at=now)
        session.add(
            BountyFeePayment(
                block_hash=fee.block_hash,
                extrinsic_index=fee.extrinsic_index,
                block_number=fee.block_number,
                claim_id=claim.claim_id,
                payer_coldkey=fee.payer_coldkey,
                amount_alpha_rao=fee.amount_alpha_rao,
                simulated=fee.simulated,
                outcome="refund_owed" if reason else "allocated",
                recorded_at=now,
            )
        )
        await session.flush()
        if reason is None:
            revision = await session.get(
                BountyRevision, (claim.bounty_id, claim.revision)
            )
            assert revision is not None
            slot.status, slot.status_changed_at = "claimed", now
            slot.deadline_at = now + timedelta(days=revision.delivery_days)
            kind = "slot_claimed"
        else:
            session.add(
                BountyObligation(
                    obligation_id=uuid4(),
                    bounty_id=claim.bounty_id,
                    claim_id=claim.claim_id,
                    kind="fee_refund",
                    reason=reason,
                    # A refund goes back to the coldkey that paid the fee.
                    payee_coldkey=fee.payer_coldkey,
                    amount_alpha_rao=fee.amount_alpha_rao,
                    block_hash=fee.block_hash,
                    extrinsic_index=fee.extrinsic_index,
                    simulated=fee.simulated,
                    created_at=now,
                )
            )
            kind = "fee_refund_owed"
        session.add(
            BountyEvent(
                bounty_id=claim.bounty_id,
                kind=kind,
                revision=claim.revision,
                round=slot.round,
                slot_id=slot.slot_id,
                claim_id=claim.claim_id,
                recorded_at=now,
            )
        )
        await session.flush()
        return await claim_view(session, claim, now=now)


async def read_claim(
    session: AsyncSession,
    claim_id: UUID,
    *,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    claim = await session.get(BountyClaim, claim_id)
    if claim is None:
        raise HTTPException(404, "bounty claim not found")
    return await claim_view(session, claim, now=(clock or utcnow)())


async def claim_view(
    session: AsyncSession, claim: BountyClaim, *, now: datetime
) -> BountyClaimView:
    bounty = await session.get(Bounty, claim.bounty_id)
    slot = await session.get(BountySlot, claim.slot_id)
    assert bounty is not None and slot is not None
    payments = list(
        await session.scalars(
            select(BountyFeePayment)
            .where(BountyFeePayment.claim_id == claim.claim_id)
            .order_by(BountyFeePayment.recorded_at, BountyFeePayment.block_hash)
        )
    )
    obligations = list(
        await session.scalars(
            select(BountyObligation)
            .where(BountyObligation.claim_id == claim.claim_id)
            .order_by(BountyObligation.created_at, BountyObligation.obligation_id)
        )
    )
    refunds = [o for o in obligations if o.kind == "fee_refund"]
    policy = await current_policy(session) if obligations else None
    facts = list(
        await session.scalars(
            select(BountyDeliveryFact)
            .where(BountyDeliveryFact.claim_id == claim.claim_id)
            .order_by(BountyDeliveryFact.fact_id)
        )
    )
    submissions = (
        await session.execute(
            select(BountySubmission, BountyReview)
            .outerjoin(
                BountyReview,
                BountyReview.submission_id == BountySubmission.submission_id,
            )
            .where(BountySubmission.claim_id == claim.claim_id)
            .order_by(BountySubmission.submitted_at, BountySubmission.submission_id)
        )
    ).all()
    appeals = (
        await session.execute(
            select(BountyAppeal, BountyAppealDecision)
            .outerjoin(
                BountyAppealDecision,
                BountyAppealDecision.appeal_id == BountyAppeal.appeal_id,
            )
            .where(BountyAppeal.claim_id == claim.claim_id)
            .order_by(BountyAppeal.filed_at, BountyAppeal.appeal_id)
        )
    ).all()
    allocated = next((p for p in payments if p.outcome == "allocated"), None)
    holds_slot = slot.claim_id == claim.claim_id
    if allocated is not None:
        state = "claimed"
    elif holds_slot and slot.status == "held" and now < claim.hold_expires_at:
        state = "held"
    elif refunds:
        state = "refund_owed"
    else:
        state = "hold_expired"
    # The claim's own slot status: whatever the slot holds now belongs to
    # another claim. A reopened claim keeps the outcome it was reopened from;
    # a released hold reads open.
    if holds_slot:
        slot_status = effective_slot_status(slot, claim, now=now)
    else:
        slot_status = (
            await session.scalar(
                select(BountySlotAction.prior_status)
                .where(
                    BountySlotAction.claim_id == claim.claim_id,
                    BountySlotAction.action == "reopen",
                )
                .order_by(BountySlotAction.action_id.desc())
                .limit(1)
            )
            or "open"
        )
    return BountyClaimView(
        claim_id=claim.claim_id,
        bounty_id=claim.bounty_id,
        repo=bounty.repo,
        issue=bounty.issue_number,
        revision=claim.revision,
        spec_digest=claim.spec_digest,
        slot_id=slot.slot_id,
        round=slot.round,
        position=slot.position,
        state=state,
        slot_status=slot_status,  # type: ignore[arg-type]
        hotkey=claim.hotkey,
        payee_coldkey=claim.payee_coldkey,
        github_login=claim.github_login,
        funding_mode=claim.funding_mode,  # type: ignore[arg-type]
        fee_alpha_rao=claim.fee_alpha_rao,
        fee_escrow_coldkey=claim.fee_escrow_coldkey,
        fee_escrow_hotkey=claim.fee_escrow_hotkey,
        held_at=claim.held_at,
        hold_expires_at=claim.hold_expires_at,
        claimed_at=allocated.recorded_at if allocated else None,
        # Only a live delivery or rework deadline: a decided, withdrawn or
        # missed claim has none left to meet.
        deadline_at=slot.deadline_at
        if allocated and holds_slot and slot_status in DEADLINE_STATUSES
        else None,
        simulated_receipt=(
            BountyFeeReceipt(
                block_hash=simulated_fee_block_hash(claim.claim_id),
                block_number=0,
                extrinsic_index=0,
            )
            if claim.funding_mode == "rehearsal"
            else None
        ),
        receipts=[
            BountyFeeReceipt(
                block_hash=p.block_hash,
                block_number=p.block_number,
                extrinsic_index=p.extrinsic_index,
            )
            for p in payments
        ],
        refunds=[
            BountyRefundView(
                obligation_id=r.obligation_id,
                reason=r.reason,  # type: ignore[arg-type]
                payee_coldkey=r.payee_coldkey,
                amount_alpha_rao=r.amount_alpha_rao,
                receipt=next(
                    BountyFeeReceipt(
                        block_hash=p.block_hash,
                        block_number=p.block_number,
                        extrinsic_index=p.extrinsic_index,
                    )
                    for p in payments
                    if (p.block_hash, p.extrinsic_index)
                    == (r.block_hash, r.extrinsic_index)
                ),
                simulated=r.simulated,
                created_at=r.created_at,
            )
            for r in refunds
        ],
        submissions=[
            BountySubmissionView(
                submission_id=sub.submission_id,
                pr_number=sub.pr_number,
                head_sha=sub.head_sha,
                submitted_at=sub.submitted_at,
                review_due_at=sub.review_due_at,
                decision=review.decision if review else None,  # type: ignore[arg-type]
                decision_reason=review.reason if review else None,
                attribution=review.attribution if review else None,
                rework_deadline_at=review.rework_deadline_at if review else None,
                decided_at=review.decided_at if review else None,
            )
            for sub, review in submissions
        ],
        appeals=[
            BountyAppealView(
                appeal_id=appeal.appeal_id,
                subject_event_id=appeal.subject_event_id,
                reason_digest=appeal.reason_digest,
                filed_at=appeal.filed_at,
                upheld=decision.upheld if decision else None,
                decided_at=decision.decided_at if decision else None,
            )
            for appeal, decision in appeals
        ],
        obligations=[
            await obligation_view(session, obligation, policy)
            for obligation in obligations
        ],
        facts=[
            BountyFactView(
                kind=fact.kind,  # type: ignore[arg-type]
                reference=fact.reference,
                recorded_at=fact.recorded_at,
            )
            for fact in facts
        ],
    )


async def set_claims_paused(
    maker: async_sessionmaker[AsyncSession],
    *,
    bounty_id: UUID,
    body: BountyClaimsPauseRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> bool:
    """Pause or resume new holds. Existing holds and payments are unaffected."""
    if not body.reason.strip():
        raise HTTPException(400, "reason must not be blank")
    now = (clock or utcnow)()
    async with maker() as session, session.begin():
        bounty = await locked_bounty(session, bounty_id)
        expected = bounty_pause_confirmation(
            repo=bounty.repo, issue=bounty.issue_number, paused=body.paused
        )
        if body.confirmation != expected:
            raise HTTPException(
                409, f"explicit confirmation is required: type '{expected}'"
            )
        if await claims_paused(session, bounty_id) == body.paused:
            state = "paused" if body.paused else "open"
            raise HTTPException(409, f"new claims are already {state}")
        session.add(
            BountyClaimControl(
                bounty_id=bounty_id,
                paused=body.paused,
                actor=actor,
                reason=body.reason,
                recorded_at=now,
            )
        )
        session.add(
            BountyEvent(
                bounty_id=bounty_id,
                kind="claims_paused" if body.paused else "claims_resumed",
                recorded_at=now,
            )
        )
    return body.paused
