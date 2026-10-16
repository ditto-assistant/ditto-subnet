"""Bounty settlement: fee dispositions, awards, approvals, transfers and facts.

Decisions create obligations; obligations move money only through approved
transfers. Each step is its own recorded fact:

- **Dispositions.** Every confirmed entry fee ends in exactly one: refunded in
  full when the work is accepted or the slot is cancelled, or retained once a
  rejected, expired or withdrawn slot is reopened, which is when that outcome
  becomes final (a reopened slot can no longer be appealed).
- **Awards.** An operator awards accepted work an amount with public credit.
  A bounty's awards never exceed its reward pool; a shared award awards each
  accepted claim.
- **Approvals.** Each approval binds the obligation, its payee, its amount and
  the payout policy revision in force. The policy says how many distinct
  operators an amount needs. Whoever decided the award, or accepted the work,
  may not approve its payout. An award's payout also holds while the
  claim hotkey's finalized owner differs from the payee of record, which may
  mean that coldkey was lost or compromised; rebinding a payee ships with
  live transfers. A fee refund always returns to the coldkey that paid it.
- **Transfers.** An approved round is dispatched once. A transfer whose outcome
  is unknown is ``uncertain`` and must be reconciled before anything else; a
  failed or expired one needs fresh approvals for the next round. Partial
  unique indexes keep at most one transfer live per obligation.
- **Facts.** Merge, deployment and adoption are recorded publicly for accepted
  work. None is ever a condition for paying it.

v1 runs rehearsals only: the transfer executor is simulated and moves no
funds, and rehearsals may ask it for a failed or unknown outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, func, select

from ditto.api_models.bounty import (
    BountyApprovalRequest,
    BountyAwardRequest,
    BountyClaimView,
    BountyDispatchRequest,
    BountyFactRequest,
    BountyObligationView,
    BountyPayoutPolicyRequest,
    BountyPayoutPolicyView,
    BountyReconcileRequest,
    bounty_approval_confirmation,
    bounty_award_confirmation,
    bounty_dispatch_confirmation,
    bounty_fact_confirmation,
    bounty_payout_policy_confirmation,
    bounty_reconcile_confirmation,
    simulated_transfer_block_hash,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_claims import claim_view, locked_bounty
from ditto.api_server.bounty_obligations import (
    current_policy,
    obligation_state,
    obligation_view,
    required_approvals,
)
from ditto.api_server.contributor_identity import (
    ContributorProofRejected,
    finalized_owner,
)
from ditto.chain import ChainError
from ditto.db.models import (
    BountyClaim,
    BountyDeliveryFact,
    BountyEvent,
    BountyFeePayment,
    BountyObligation,
    BountyObligationApproval,
    BountyPayoutPolicy,
    BountyReview,
    BountyRevision,
    BountySlot,
    BountySubmission,
    BountyTransfer,
    BountyTransferReconciliation,
)
from ditto.db.queries.contributor_identity import lock_identity_writes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)
_POLICY_LOCK = "bounty-payout-policy"
# Final outcomes whose entry fee is retained when the slot is reopened.
RETAINED_OUTCOMES = ("rejected", "expired", "withdrawn")


@dataclass(frozen=True)
class TransferOutcome:
    """What an executor reports: a settled state, or ``uncertain``."""

    state: str
    block_hash: str | None = None
    block_number: int | None = None
    extrinsic_index: int | None = None


class BountyTransferExecutor(Protocol):
    """Sends a transfer and later reports what became of it.

    A settled outcome is final: the schema refuses to change a transfer once
    it is ``confirmed``, ``failed`` or ``expired``, so a retry round can
    never coexist with a late success. An executor therefore reports
    ``failed`` or ``expired`` only when the transfer can no longer land (for
    example, its extrinsic's mortality has passed), and ``uncertain`` until
    then, from ``execute`` and ``lookup`` alike.
    """

    name: str
    simulated: bool

    async def execute(
        self, transfer_id: UUID, obligation: BountyObligation, /, *, rehearsal: str
    ) -> TransferOutcome: ...

    async def lookup(
        self, transfer_id: UUID, obligation: BountyObligation, /, *, rehearsal: str
    ) -> TransferOutcome: ...


class SimulatedTransferExecutor:
    """Rehearsal transfers: a deterministic receipt per transfer, no funds.

    A rehearsal may ask for a failed or unknown outcome to practise those
    paths; a live executor takes no such instruction.
    """

    name = "simulated"
    simulated = True
    _STATES = {
        "confirmed": "confirmed",
        "failed": "failed",
        "expired": "expired",
        "unknown": "uncertain",
    }

    async def execute(
        self, transfer_id: UUID, _obligation: BountyObligation, /, *, rehearsal: str
    ) -> TransferOutcome:
        return self._outcome(transfer_id, rehearsal)

    async def lookup(
        self, transfer_id: UUID, _obligation: BountyObligation, /, *, rehearsal: str
    ) -> TransferOutcome:
        return self._outcome(transfer_id, rehearsal)

    def _outcome(self, transfer_id: UUID, rehearsal: str) -> TransferOutcome:
        state = self._STATES[rehearsal]
        if state != "confirmed":
            return TransferOutcome(state)
        return TransferOutcome(
            "confirmed", simulated_transfer_block_hash(transfer_id), 0, 0
        )


def _now(clock: Callable[[], datetime] | None) -> datetime:
    return (clock or bounty_claims.utcnow)()


def _confirm(given: str, expected: str) -> None:
    if given != expected:
        raise HTTPException(
            409, f"explicit confirmation is required: type '{expected}'"
        )


def _event(
    session: AsyncSession,
    claim: BountyClaim,
    kind: str,
    at: datetime,
    *,
    obligation_id: UUID | None = None,
    slot: BountySlot | None = None,
) -> None:
    session.add(
        BountyEvent(
            bounty_id=claim.bounty_id,
            kind=kind,
            revision=claim.revision,
            round=slot.round if slot else None,
            slot_id=slot.slot_id if slot else claim.slot_id,
            claim_id=claim.claim_id,
            obligation_id=obligation_id,
            recorded_at=at,
        )
    )


async def record_fee_disposition(
    session: AsyncSession,
    *,
    claim: BountyClaim,
    slot: BountySlot,
    kind: str,
    reason: str,
    now: datetime,
) -> BountyObligation | None:
    """The one final disposition of a claim's confirmed fee, recorded once."""
    payment = await session.scalar(
        select(BountyFeePayment).where(
            BountyFeePayment.claim_id == claim.claim_id,
            BountyFeePayment.outcome == "allocated",
        )
    )
    if payment is None:
        return None
    existing = await session.scalar(
        select(BountyObligation).where(
            BountyObligation.block_hash == payment.block_hash,
            BountyObligation.extrinsic_index == payment.extrinsic_index,
            BountyObligation.kind.in_(("fee_refund", "fee_retained")),
        )
    )
    if existing is not None:
        if existing.kind != kind:
            # A fee has one final disposition; a different one is a conflict,
            # not a retry the caller can proceed past.
            outcome = "refunded" if existing.kind == "fee_refund" else "retained"
            raise HTTPException(409, f"this entry fee was already {outcome}")
        return existing
    obligation = BountyObligation(
        obligation_id=uuid4(),
        bounty_id=claim.bounty_id,
        claim_id=claim.claim_id,
        kind=kind,
        reason=reason,
        # A fee goes back to, or is retained from, the coldkey that paid it.
        payee_coldkey=payment.payer_coldkey,
        amount_alpha_rao=payment.amount_alpha_rao,
        block_hash=payment.block_hash,
        extrinsic_index=payment.extrinsic_index,
        simulated=payment.simulated,
        created_at=now,
    )
    session.add(obligation)
    await session.flush()
    event = "fee_refund_owed" if kind == "fee_refund" else "fee_retained"
    _event(
        session, claim, event, now, obligation_id=obligation.obligation_id, slot=slot
    )
    return obligation


async def award_claim(
    maker: async_sessionmaker[AsyncSession],
    *,
    claim_id: UUID,
    body: BountyAwardRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Award accepted work from the reward pool, with public credit."""
    if not body.reason.strip() or not body.attribution.strip():
        raise HTTPException(400, "reason and attribution must not be blank")
    now = _now(clock)
    async with maker() as session, session.begin():
        claim = await session.get(BountyClaim, claim_id)
        if claim is None:
            raise HTTPException(404, "bounty claim not found")
        await locked_bounty(session, claim.bounty_id)
        _confirm(
            body.confirmation,
            bounty_award_confirmation(
                claim_id=claim_id, amount_alpha_rao=body.amount_alpha_rao
            ),
        )
        existing = await session.scalar(
            select(BountyObligation).where(
                BountyObligation.claim_id == claim_id, BountyObligation.kind == "award"
            )
        )
        if existing is not None:
            if (
                existing.amount_alpha_rao,
                existing.attribution,
                existing.decision_reason,
            ) == (body.amount_alpha_rao, body.attribution, body.reason):
                return await claim_view(session, claim, now=now)
            raise HTTPException(409, "this claim was already awarded")
        slot = await session.get(BountySlot, claim.slot_id)
        assert slot is not None
        accepted = await session.scalar(
            select(BountySubmission.submission_id)
            .join(
                BountyReview,
                BountyReview.submission_id == BountySubmission.submission_id,
            )
            .where(
                BountySubmission.claim_id == claim_id,
                BountyReview.decision == "accept",
            )
        )
        if slot.claim_id != claim_id or slot.status != "accepted" or accepted is None:
            raise HTTPException(409, "only accepted work can be awarded")
        pool = await session.scalar(
            select(BountyRevision.reward_pool_alpha_rao)
            .where(BountyRevision.bounty_id == claim.bounty_id)
            .order_by(BountyRevision.revision.desc())
            .limit(1)
        )
        awarded = int(
            await session.scalar(
                select(
                    func.coalesce(func.sum(BountyObligation.amount_alpha_rao), 0)
                ).where(
                    BountyObligation.bounty_id == claim.bounty_id,
                    BountyObligation.kind == "award",
                )
            )
        )
        assert pool is not None
        if awarded + body.amount_alpha_rao > pool:
            raise HTTPException(
                409,
                "awards would exceed the reward pool: "
                f"{pool - awarded} alpha rao remain",
            )
        obligation = BountyObligation(
            obligation_id=uuid4(),
            bounty_id=claim.bounty_id,
            claim_id=claim_id,
            kind="award",
            reason="accepted_work",
            payee_coldkey=claim.payee_coldkey,
            amount_alpha_rao=body.amount_alpha_rao,
            simulated=claim.funding_mode == "rehearsal",
            submission_id=accepted,
            attribution=body.attribution,
            actor=actor,
            decision_reason=body.reason,
            created_at=now,
        )
        session.add(obligation)
        await session.flush()
        _event(
            session,
            claim,
            "award_recorded",
            now,
            obligation_id=obligation.obligation_id,
            slot=slot,
        )
        await session.flush()
        return await claim_view(session, claim, now=now)


def policy_view(policy: BountyPayoutPolicy) -> BountyPayoutPolicyView:
    return BountyPayoutPolicyView(
        revision=policy.revision,
        single_approver_max_alpha_rao=policy.single_approver_max_alpha_rao,
        dual_approver_max_alpha_rao=policy.dual_approver_max_alpha_rao,
        actor=policy.actor,
        reason=policy.reason,
        created_at=policy.created_at,
    )


async def read_payout_policy(
    session: AsyncSession, *, revision: int | None = None
) -> BountyPayoutPolicyView:
    """The current policy, or one exact revision so a writer can find its own."""
    if revision is not None:
        exact = await session.get(BountyPayoutPolicy, revision)
        if exact is None:
            raise HTTPException(
                404, f"bounty payout policy revision {revision} not found"
            )
        return policy_view(exact)
    policy = await current_policy(session)
    if policy is None:
        raise HTTPException(404, "no bounty payout policy is configured")
    return policy_view(policy)


async def set_payout_policy(
    maker: async_sessionmaker[AsyncSession],
    *,
    body: BountyPayoutPolicyRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyPayoutPolicyView:
    """Append a payout policy revision; approvals bind the one in force."""
    if not body.reason.strip():
        raise HTTPException(400, "reason must not be blank")
    if body.dual_approver_max_alpha_rao < body.single_approver_max_alpha_rao:
        raise HTTPException(
            400, "the dual-approver limit cannot be below the single-approver limit"
        )
    now = _now(clock)
    async with maker() as session, session.begin():
        await lock_identity_writes(session, namespace=_POLICY_LOCK)
        policy = await current_policy(session)
        current = policy.revision if policy else 0
        if current != body.expected_revision:
            # An exact retry of the write that made the current revision.
            if (
                policy is not None
                and current == body.expected_revision + 1
                and (
                    policy.single_approver_max_alpha_rao,
                    policy.dual_approver_max_alpha_rao,
                    policy.actor,
                    policy.reason,
                )
                == (
                    body.single_approver_max_alpha_rao,
                    body.dual_approver_max_alpha_rao,
                    actor,
                    body.reason,
                )
            ):
                return policy_view(policy)
            raise HTTPException(
                409, f"the payout policy changed; its revision is {current}"
            )
        _confirm(
            body.confirmation, bounty_payout_policy_confirmation(revision=current + 1)
        )
        row = BountyPayoutPolicy(
            revision=current + 1,
            single_approver_max_alpha_rao=body.single_approver_max_alpha_rao,
            dual_approver_max_alpha_rao=body.dual_approver_max_alpha_rao,
            actor=actor,
            reason=body.reason,
            created_at=now,
        )
        session.add(row)
        await session.flush()
        return policy_view(row)


async def _locked_obligation(
    session: AsyncSession, obligation_id: UUID
) -> tuple[BountyObligation, BountyClaim]:
    obligation = await session.get(BountyObligation, obligation_id)
    if obligation is None:
        raise HTTPException(404, "bounty obligation not found")
    claim = await session.get(BountyClaim, obligation.claim_id)
    assert claim is not None
    # One bounty lock orders every settlement step with the slot changes.
    await locked_bounty(session, obligation.bounty_id)
    return obligation, claim


async def _award_owner(
    maker: async_sessionmaker[AsyncSession],
    chain: ChainClient,
    obligation_id: UUID,
) -> str | None:
    """For an award, its claim hotkey's finalized owner (None if it has none).

    Read before the settlement transaction, as claims do, so no lock is held
    across the chain call. Other obligations never hold, so they skip it.
    """
    async with maker() as session:
        obligation = await session.get(BountyObligation, obligation_id)
        if obligation is None:
            raise HTTPException(404, "bounty obligation not found")
        if obligation.kind != "award":
            return None
        claim = await session.get(BountyClaim, obligation.claim_id)
        assert claim is not None
        hotkey = claim.hotkey
    try:
        return (await finalized_owner(chain, hotkey)).coldkey
    except ContributorProofRejected:
        return None
    except ChainError:
        raise HTTPException(503, "finalized ownership unavailable; retry") from None


def _hold_award(
    obligation: BountyObligation, claim: BountyClaim, owner: str | None
) -> None:
    """An award's payout holds while the hotkey's owner is not its payee."""
    if obligation.kind == "award" and owner != claim.payee_coldkey:
        raise HTTPException(
            409,
            "payout held: the claim hotkey's finalized owner is "
            f"{owner or 'missing'}, not the payee of record {claim.payee_coldkey}",
        )


async def _conflicted(
    session: AsyncSession, obligation: BountyObligation, actor: str
) -> bool:
    """Whether the actor decided this money: the award or the acceptance."""
    if obligation.actor == actor:
        return True
    # Any acceptance this actor recorded for the claim, not just the first.
    return bool(
        await session.scalar(
            select(
                exists()
                .where(BountySubmission.submission_id == BountyReview.submission_id)
                .where(
                    BountySubmission.claim_id == obligation.claim_id,
                    BountyReview.decision == "accept",
                    BountyReview.reviewer_actor == actor,
                )
            )
        )
    )


async def approve_obligation(
    maker: async_sessionmaker[AsyncSession],
    *,
    chain: ChainClient,
    obligation_id: UUID,
    body: BountyApprovalRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyObligationView:
    """One operator's approval of the exact payee, amount and policy."""
    now = _now(clock)
    owner = await _award_owner(maker, chain, obligation_id)
    async with maker() as session, session.begin():
        obligation, claim = await _locked_obligation(session, obligation_id)
        if obligation.kind == "fee_retained":
            raise HTTPException(409, "a retained fee moves no money")
        policy = await current_policy(session)
        if policy is None:
            raise HTTPException(409, "no bounty payout policy is configured")
        state = await obligation_state(session, obligation, policy)
        if state.state in ("dispatched", "uncertain", "confirmed"):
            raise HTTPException(409, f"this obligation is already {state.state}")
        if body.round != state.round:
            raise HTTPException(409, f"approve round {state.round} of this obligation")
        if (body.payee_coldkey, body.amount_alpha_rao) != (
            obligation.payee_coldkey,
            obligation.amount_alpha_rao,
        ):
            raise HTTPException(
                409, "an approval must bind the obligation's exact payee and amount"
            )
        if body.policy_revision != policy.revision:
            raise HTTPException(
                409,
                f"the payout policy changed; approve under revision {policy.revision}",
            )
        if required_approvals(obligation.amount_alpha_rao, policy) is None:
            raise HTTPException(
                409, "the amount exceeds what the payout policy lets anyone approve"
            )
        _confirm(
            body.confirmation,
            bounty_approval_confirmation(
                obligation_id=obligation_id,
                round_number=body.round,
                amount_alpha_rao=body.amount_alpha_rao,
                payee=body.payee_coldkey,
            ),
        )
        if await _conflicted(session, obligation, actor):
            raise HTTPException(
                403,
                "whoever decided this award or accepted this work cannot approve it",
            )
        already = await session.scalar(
            select(BountyObligationApproval.approval_id).where(
                BountyObligationApproval.obligation_id == obligation_id,
                BountyObligationApproval.round == body.round,
                BountyObligationApproval.actor == actor,
                BountyObligationApproval.policy_revision == policy.revision,
            )
        )
        if already is None:
            _hold_award(obligation, claim, owner)
            session.add(
                BountyObligationApproval(
                    approval_id=uuid4(),
                    obligation_id=obligation_id,
                    round=body.round,
                    payee_coldkey=body.payee_coldkey,
                    amount_alpha_rao=body.amount_alpha_rao,
                    policy_revision=policy.revision,
                    actor=actor,
                    approved_at=now,
                )
            )
            _event(
                session, claim, "obligation_approved", now, obligation_id=obligation_id
            )
            await session.flush()
        return await obligation_view(session, obligation, policy)


def _recordable(outcome: TransferOutcome, transfer_id: UUID) -> TransferOutcome:
    """The executor's outcome if a transfer can record it, else ``uncertain``.

    An unknown state, or a confirmation without its receipt, says nothing
    reliable about the money, so the transfer waits for reconciliation.
    """
    receipt = (outcome.block_hash, outcome.block_number, outcome.extrinsic_index)
    if outcome.state in ("failed", "expired", "uncertain") or (
        outcome.state == "confirmed" and None not in receipt
    ):
        return outcome
    logger.error(
        "bounty transfer %s: executor reported unrecordable %r; uncertain",
        transfer_id,
        outcome,
    )
    return TransferOutcome("uncertain")


async def _record_outcome(
    maker: async_sessionmaker[AsyncSession],
    *,
    transfer_id: UUID,
    outcome: TransferOutcome,
    now: datetime,
    reconciled_by: str | None = None,
    requested: str | None = None,
) -> BountyObligationView:
    outcome = _recordable(outcome, transfer_id)
    async with maker() as session, session.begin():
        transfer = await session.get(BountyTransfer, transfer_id)
        assert transfer is not None
        obligation, claim = await _locked_obligation(session, transfer.obligation_id)
        transfer = await session.scalar(
            select(BountyTransfer)
            .where(BountyTransfer.transfer_id == transfer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert transfer is not None
        # Another request may have settled it first; never move it twice.
        settled = transfer.state not in ("dispatched", "uncertain")
        if settled and outcome.state not in (transfer.state, "uncertain"):
            # An executor contradicting a final outcome broke its contract;
            # the record stays, but an operator must look at the chain.
            logger.error(
                "bounty transfer %s is %s, but its executor now reports %r",
                transfer_id,
                transfer.state,
                outcome,
            )
        if not settled and outcome.state != transfer.state:
            transfer.state = outcome.state
            if outcome.state != "uncertain":
                transfer.settled_at = now
            if outcome.state == "confirmed":
                transfer.block_hash = outcome.block_hash
                transfer.block_number = outcome.block_number
                transfer.extrinsic_index = outcome.extrinsic_index
            kind = {
                "confirmed": "transfer_confirmed",
                "failed": "transfer_failed",
                "expired": "transfer_failed",
                "uncertain": "transfer_uncertain",
            }[outcome.state]
            _event(session, claim, kind, now, obligation_id=obligation.obligation_id)
        if reconciled_by is not None:
            # Every reconciliation is recorded with its operator, including a
            # check that leaves the transfer uncertain.
            session.add(
                BountyTransferReconciliation(
                    transfer_id=transfer_id,
                    actor=reconciled_by,
                    requested=requested,
                    state=transfer.state,
                    recorded_at=now,
                )
            )
        await session.flush()
        return await obligation_view(session, obligation, await current_policy(session))


async def dispatch_obligation(
    maker: async_sessionmaker[AsyncSession],
    *,
    chain: ChainClient,
    executor: BountyTransferExecutor,
    obligation_id: UUID,
    body: BountyDispatchRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyObligationView:
    """Dispatch an approved round once; record the outcome, or uncertainty."""
    if not executor.simulated and body.rehearsal_outcome != "confirmed":
        raise HTTPException(400, "only a rehearsal executor takes a rehearsal outcome")
    now = _now(clock)
    owner = await _award_owner(maker, chain, obligation_id)
    async with maker() as session, session.begin():
        obligation, claim = await _locked_obligation(session, obligation_id)
        if obligation.simulated != executor.simulated:
            raise HTTPException(
                409, "this obligation and the transfer executor differ in funding mode"
            )
        state = await obligation_state(
            session, obligation, await current_policy(session)
        )
        if state.state != "approved":
            raise HTTPException(
                409, f"only an approved obligation is dispatched; it is {state.state}"
            )
        if body.round != state.round:
            raise HTTPException(409, f"dispatch round {state.round} of this obligation")
        _confirm(
            body.confirmation,
            bounty_dispatch_confirmation(
                obligation_id=obligation_id, round_number=body.round
            ),
        )
        _hold_award(obligation, claim, owner)
        transfer = BountyTransfer(
            transfer_id=uuid4(),
            obligation_id=obligation_id,
            round=body.round,
            executor=executor.name,
            simulated=executor.simulated,
            state="dispatched",
            actor=actor,
            dispatched_at=now,
        )
        session.add(transfer)
        _event(session, claim, "transfer_dispatched", now, obligation_id=obligation_id)
        await session.flush()
        transfer_id = transfer.transfer_id
        session.expunge(obligation)
    # The dispatch is recorded before anything is sent, so a crash leaves a
    # transfer to reconcile, never one to send twice.
    try:
        outcome = await executor.execute(
            transfer_id, obligation, rehearsal=body.rehearsal_outcome
        )
    except Exception:
        # Whatever failed, the transfer may have been sent: record it uncertain
        # for reconciliation, and keep the failure for diagnosis.
        logger.exception("bounty transfer %s execute failed; uncertain", transfer_id)
        outcome = TransferOutcome("uncertain")
    return await _record_outcome(
        maker, transfer_id=transfer_id, outcome=outcome, now=_now(clock)
    )


async def reconcile_transfer(
    maker: async_sessionmaker[AsyncSession],
    *,
    executor: BountyTransferExecutor,
    transfer_id: UUID,
    body: BountyReconcileRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyObligationView:
    """Settle a dispatched or uncertain transfer from what the chain shows."""
    if not executor.simulated and body.rehearsal_outcome != "confirmed":
        raise HTTPException(400, "only a rehearsal executor takes a rehearsal outcome")
    async with maker() as session, session.begin():
        transfer = await session.get(BountyTransfer, transfer_id)
        if transfer is None:
            raise HTTPException(404, "bounty transfer not found")
        obligation, _claim = await _locked_obligation(session, transfer.obligation_id)
        _confirm(
            body.confirmation, bounty_reconcile_confirmation(transfer_id=transfer_id)
        )
        if transfer.state not in ("dispatched", "uncertain"):
            raise HTTPException(409, f"this transfer is already {transfer.state}")
        # Only an executor of the transfer's own funding mode can say what
        # became of it, as only one could have sent it.
        if transfer.simulated != executor.simulated:
            raise HTTPException(
                409, "this transfer and the transfer executor differ in funding mode"
            )
        session.expunge(obligation)
    try:
        outcome = await executor.lookup(
            transfer_id, obligation, rehearsal=body.rehearsal_outcome
        )
    except Exception:
        logger.exception("bounty transfer %s lookup failed; uncertain", transfer_id)
        outcome = TransferOutcome("uncertain")
    return await _record_outcome(
        maker,
        transfer_id=transfer_id,
        outcome=outcome,
        now=_now(clock),
        reconciled_by=actor,
        requested=body.rehearsal_outcome if executor.simulated else None,
    )


async def record_fact(
    maker: async_sessionmaker[AsyncSession],
    *,
    claim_id: UUID,
    body: BountyFactRequest,
    actor: str,
    clock: Callable[[], datetime] | None = None,
) -> BountyClaimView:
    """Record a public merge, deployment or adoption fact for accepted work."""
    if not body.reference.strip():
        raise HTTPException(400, "reference must not be blank")
    now = _now(clock)
    async with maker() as session, session.begin():
        claim = await session.get(BountyClaim, claim_id)
        if claim is None:
            raise HTTPException(404, "bounty claim not found")
        await locked_bounty(session, claim.bounty_id)
        _confirm(
            body.confirmation,
            bounty_fact_confirmation(claim_id=claim_id, kind=body.kind),
        )
        existing = await session.scalar(
            select(BountyDeliveryFact).where(
                BountyDeliveryFact.claim_id == claim_id,
                BountyDeliveryFact.kind == body.kind,
            )
        )
        if existing is not None:
            if existing.reference == body.reference:
                return await claim_view(session, claim, now=now)
            raise HTTPException(
                409, f"this claim's {body.kind} fact is already recorded"
            )
        slot = await session.get(BountySlot, claim.slot_id)
        assert slot is not None
        if slot.claim_id != claim_id or slot.status != "accepted":
            raise HTTPException(409, "delivery facts are recorded for accepted work")
        session.add(
            BountyDeliveryFact(
                claim_id=claim_id,
                kind=body.kind,
                reference=body.reference,
                actor=actor,
                recorded_at=now,
            )
        )
        _event(session, claim, "fact_recorded", now, slot=slot)
        await session.flush()
        return await claim_view(session, claim, now=now)
