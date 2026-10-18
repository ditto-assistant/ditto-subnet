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
  ``awaiting_approval`` before that;
- an amount above the policy's dual-approver limit is ``over_policy_limit``:
  nobody can approve it until a policy revision raises the limit, so this
  outranks ``failed``, whose fresh approvals could not arrive. With no
  policy configured yet, the round awaits approval with no known requirement.
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
    from uuid import UUID

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
    # The schema keeps single <= dual; bounding it here too means no policy
    # can ever let one operator approve more than the dual cap.
    if amount <= min(
        policy.single_approver_max_alpha_rao, policy.dual_approver_max_alpha_rao
    ):
        return 1
    if amount <= policy.dual_approver_max_alpha_rao:
        return 2
    return None


@dataclass(frozen=True)
class ObligationState:
    state: str
    round: int
    approvals: int
    required: int | None
    transfers: list[BountyTransfer]


async def obligation_state(
    session: AsyncSession,
    obligation: BountyObligation,
    policy: BountyPayoutPolicy | None,
) -> ObligationState:
    return (await obligation_states(session, [obligation], policy))[
        obligation.obligation_id
    ]


async def obligation_states(
    session: AsyncSession,
    obligations: list[BountyObligation],
    policy: BountyPayoutPolicy | None,
) -> dict[UUID, ObligationState]:
    """Each obligation's state, in two reads however many obligations there are."""
    settled = [o.obligation_id for o in obligations if o.kind != "fee_retained"]
    transfers: dict[UUID, list[BountyTransfer]] = {}
    # Distinct operators who approved each round under the policy in force.
    approvals: dict[tuple[UUID, int], int] = {}
    if settled:
        for transfer in await session.scalars(
            select(BountyTransfer)
            .where(BountyTransfer.obligation_id.in_(settled))
            .order_by(BountyTransfer.round)
        ):
            transfers.setdefault(transfer.obligation_id, []).append(transfer)
    if settled and policy is not None:
        approvals = {
            (obligation_id, round_number): int(count)
            for obligation_id, round_number, count in await session.execute(
                select(
                    BountyObligationApproval.obligation_id,
                    BountyObligationApproval.round,
                    func.count(func.distinct(BountyObligationApproval.actor)),
                )
                .where(
                    BountyObligationApproval.obligation_id.in_(settled),
                    BountyObligationApproval.policy_revision == policy.revision,
                )
                .group_by(
                    BountyObligationApproval.obligation_id,
                    BountyObligationApproval.round,
                )
            )
        }
    return {
        o.obligation_id: _derive(
            o, transfers.get(o.obligation_id, []), approvals, policy
        )
        for o in obligations
    }


def _derive(
    obligation: BountyObligation,
    transfers: list[BountyTransfer],
    approved: dict[tuple[UUID, int], int],
    policy: BountyPayoutPolicy | None,
) -> ObligationState:
    if obligation.kind == "fee_retained":
        return ObligationState("retained", 0, 0, 0, [])
    latest = transfers[-1] if transfers else None
    if latest is not None and latest.state in LIVE_TRANSFER_STATES:
        return ObligationState(
            latest.state,
            latest.round,
            approved.get((obligation.obligation_id, latest.round), 0),
            required_approvals(obligation.amount_alpha_rao, policy),
            transfers,
        )
    round_number = latest.round + 1 if latest is not None else 1
    approvals = approved.get((obligation.obligation_id, round_number), 0)
    required = required_approvals(obligation.amount_alpha_rao, policy)
    if policy is not None and required is None:
        state = "over_policy_limit"
    elif required is not None and approvals >= required:
        state = "approved"
    elif latest is not None and approvals == 0:
        # A failed or expired transfer needs fresh approvals before any retry.
        state = "failed"
    else:
        state = "awaiting_approval"
    return ObligationState(state, round_number, approvals, required, transfers)


def transfer_view(transfer: BountyTransfer) -> BountyTransferView:
    return BountyTransferView(
        transfer_id=transfer.transfer_id,
        round=transfer.round,
        state=transfer.state,  # type: ignore[arg-type]
        receipt=(
            BountyFeeReceipt(
                block_hash=transfer.block_hash,
                block_number=transfer.block_number,
                extrinsic_index=transfer.extrinsic_index,
            )
            # The schema records all three or none; never fill one in.
            if transfer.block_hash is not None
            and transfer.block_number is not None
            and transfer.extrinsic_index is not None
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
    *,
    state: ObligationState | None = None,
) -> BountyObligationView:
    """The public view; pass a ``state`` already derived to skip deriving it."""
    if state is None:
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
