"""What a bounty obligation's settlement state is, and how it reads publicly.

An obligation's state is derived, never stored, from its approvals and its
transfers, so no write can leave the two disagreeing:

- a retained fee moves no money and is simply ``retained``;
- a live transfer gives its own state (``dispatched``, ``uncertain`` or
  ``confirmed``);
- after a failed or expired transfer the next round needs fresh approvals,
  and until the first arrives the obligation reads ``failed``;
- otherwise the round is ``approved`` once the payout policy's required number
  of distinct operators approved it under the revision in force, and
  ``awaiting_approval`` before that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ditto.api_models.bounty import (
    BountyFeeReceipt,
    BountyObligationView,
    BountyTransferView,
)
from ditto.db.models import (
    BountyObligation,
    BountyObligationApproval,
    BountyPayoutPolicy,
    BountyTransfer,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

LIVE_TRANSFER_STATES = ("dispatched", "uncertain", "confirmed")
SETTLED_FAILURES = ("failed", "expired")


async def current_policy(session: AsyncSession) -> BountyPayoutPolicy | None:
    return await session.scalar(
        select(BountyPayoutPolicy).order_by(BountyPayoutPolicy.revision.desc()).limit(1)
    )


def required_approvals(amount: int, policy: BountyPayoutPolicy | None) -> int | None:
    """Distinct approvers an amount needs; ``None`` when nobody may approve it."""
    if policy is None:
        return None
    if amount <= policy.single_approver_max_alpha_rao:
        return 1
    if amount <= policy.dual_approver_max_alpha_rao:
        return 2
    return None


@dataclass(frozen=True)
class ObligationState:
    state: str
    round: int
    approvals: int
    required: int
    transfers: list[BountyTransfer]


async def obligation_state(
    session: AsyncSession,
    obligation: BountyObligation,
    policy: BountyPayoutPolicy | None,
) -> ObligationState:
    if obligation.kind == "fee_retained":
        return ObligationState("retained", 0, 0, 0, [])
    transfers = list(
        await session.scalars(
            select(BountyTransfer)
            .where(BountyTransfer.obligation_id == obligation.obligation_id)
            .order_by(BountyTransfer.round)
        )
    )
    latest = transfers[-1] if transfers else None
    if latest is not None and latest.state in LIVE_TRANSFER_STATES:
        return ObligationState(
            latest.state,
            latest.round,
            await _approvals(session, obligation, latest.round, policy),
            required_approvals(obligation.amount_alpha_rao, policy) or 0,
            transfers,
        )
    round_number = latest.round + 1 if latest is not None else 1
    approvals = await _approvals(session, obligation, round_number, policy)
    required = required_approvals(obligation.amount_alpha_rao, policy)
    if required is not None and approvals >= required:
        state = "approved"
    elif latest is not None and approvals == 0:
        # A failed or expired transfer needs fresh approvals before any retry.
        state = "failed"
    else:
        state = "awaiting_approval"
    return ObligationState(state, round_number, approvals, required or 0, transfers)


async def _approvals(
    session: AsyncSession,
    obligation: BountyObligation,
    round_number: int,
    policy: BountyPayoutPolicy | None,
) -> int:
    """Distinct operators who approved this round under the policy in force."""
    if policy is None:
        return 0
    return int(
        await session.scalar(
            select(func.count(func.distinct(BountyObligationApproval.actor))).where(
                BountyObligationApproval.obligation_id == obligation.obligation_id,
                BountyObligationApproval.round == round_number,
                BountyObligationApproval.policy_revision == policy.revision,
            )
        )
        or 0
    )


def transfer_view(transfer: BountyTransfer) -> BountyTransferView:
    return BountyTransferView(
        transfer_id=transfer.transfer_id,
        round=transfer.round,
        state=transfer.state,  # type: ignore[arg-type]
        receipt=(
            BountyFeeReceipt(
                block_hash=transfer.block_hash,
                block_number=transfer.block_number or 0,
                extrinsic_index=transfer.extrinsic_index or 0,
            )
            if transfer.block_hash is not None
            else None
        ),
        simulated=transfer.simulated,
        dispatched_at=transfer.dispatched_at,
        settled_at=transfer.settled_at,
    )


async def obligation_view(
    session: AsyncSession,
    obligation: BountyObligation,
    policy: BountyPayoutPolicy | None,
) -> BountyObligationView:
    state = await obligation_state(session, obligation, policy)
    return BountyObligationView(
        obligation_id=obligation.obligation_id,
        kind=obligation.kind,  # type: ignore[arg-type]
        reason=obligation.reason,  # type: ignore[arg-type]
        payee_coldkey=obligation.payee_coldkey,
        amount_alpha_rao=obligation.amount_alpha_rao,
        state=state.state,  # type: ignore[arg-type]
        round=state.round,
        approvals=state.approvals,
        approvals_required=state.required,
        attribution=obligation.attribution,
        simulated=obligation.simulated,
        created_at=obligation.created_at,
        transfers=[transfer_view(transfer) for transfer in state.transfers],
    )
