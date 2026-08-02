"""ATH review + dispute surface.

Everything the operator review console and the public ``review=ath`` activity
lane can render: pending / reopened / resolved ATH reviews (with evidence and
action history), resolved-reject quarantines with a pending and a resolved
screening dispute, and enough finalized agents that ``preserved_composite``
and the leaderboard have context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    AthReview,
    AthReviewAction,
    ScreeningDispute,
    ScreeningQuarantineResolution,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.db.models import Agent
    from ditto.simulator.fabric import Fabric
    from ditto.simulator.scenarios import ScenarioContext

NAME = "reviews"
DESCRIPTION = (
    "ATH reviews (pending, reopened, resolved clear/reject) + quarantine "
    "disputes over a finalized leaderboard."
)

_ACTOR = "operator:sim-admin"


def _evidence(similarity: float) -> dict[str, object]:
    return {
        "content_similarity": similarity,
        "size_delta_bytes": 412,
        "channel": "content_fingerprint",
    }


_PROVENANCE: dict[str, object] = {
    "algorithm": "minhash-jaccard",
    "k": 256,
    "threshold": 0.9,
}


async def _resolved_review(
    session: AsyncSession,
    f: Fabric,
    *,
    agent: Agent,
    original: Agent,
    resolution: str,
    reason: str,
    opened_days_ago: float,
    resolved_hours_ago: float,
) -> AthReview:
    """A review that already ran its course, with its action-history row."""
    review = AthReview(
        review_id=f.uuid(f"ath-review:{agent.agent_id}"),
        agent_id=agent.agent_id,
        status="resolved",
        opened_at=f.days_ago(opened_days_ago),
        resolved_at=f.hours_ago(resolved_hours_ago),
        resolved_by=_ACTOR,
        resolution=resolution,
        resolution_reason=reason,
        original_duplicate_of=original.agent_id,
        original_reason=(
            f"anti-copy hold: near-duplicate of {original.name} v{original.version}"
        ),
        original_policy_version=SCREENING_POLICY_VERSION,
        original_evidence=_evidence(0.94 if resolution == "clear" else 0.99),
        algorithm_provenance=_PROVENANCE,
    )
    session.add(review)
    await session.flush()
    session.add(
        AthReviewAction(
            action_id=f.uuid(f"ath-action:{review.review_id}:{resolution}"),
            review_id=review.review_id,
            action=resolution,
            reason=reason,
            actor=_ACTOR,
            evidence=_evidence(0.94 if resolution == "clear" else 0.99),
            created_at=review.resolved_at or f.now,
        )
    )
    await session.flush()
    return review


async def _rejected_with_disputed_quarantine(
    session: AsyncSession,
    f: Fabric,
    *,
    index: int,
    reason_code: str,
    dispute_message: str,
    dispute_resolved: bool,
) -> None:
    """Rejected agent whose quarantine was resolved ``reject``, plus a dispute."""
    agent, quarantine = await f.quarantined_agent(
        session, index=index, reason_code=reason_code
    )
    agent.status = AgentStatus.REJECTED
    agent.screening_reason = "policy violation"
    agent.screening_reason_code = reason_code
    resolved_at = f.hours_ago(20 + index)
    quarantine.status = "resolved"
    quarantine.resolved_at = resolved_at
    quarantine.resolved_by = _ACTOR
    quarantine.resolution = "reject"
    quarantine.resolution_reason = (
        "screener evidence confirmed; submission rejected under policy"
    )
    session.add(
        ScreeningQuarantineResolution(
            resolution_id=f.uuid(f"quarantine-resolution:{quarantine.quarantine_id}"),
            quarantine_id=quarantine.quarantine_id,
            resolution="reject",
            reason=quarantine.resolution_reason,
            actor=_ACTOR,
            created_at=resolved_at,
        )
    )
    await session.flush()
    dispute = ScreeningDispute(
        dispute_id=f.uuid(f"dispute:{agent.agent_id}"),
        agent_id=agent.agent_id,
        quarantine_id=quarantine.quarantine_id,
        miner_hotkey=agent.miner_hotkey,
        message=dispute_message,
        status="resolved" if dispute_resolved else "pending",
        created_at=f.hours_ago(10 + index),
    )
    if dispute_resolved:
        dispute.resolved_at = f.hours_ago(2)
        dispute.resolved_by = _ACTOR
        dispute.resolution = "release"
        dispute.resolution_reason = (
            "miner evidence checks out; flagged path is unreachable at runtime"
        )
    session.add(dispute)
    await session.flush()


async def apply(ctx: ScenarioContext) -> None:
    f = ctx.fabric
    async with ctx.session_maker() as session, session.begin():
        for validator in ("validator-1", "validator-2", "validator-3"):
            await f.validator_heartbeat(session, name=validator)
        await f.screener_heartbeat(session, name="screener-1")

        # Leaderboard backdrop: five finalized miners so preserved_composite
        # has real neighbors and the KOTH/floor math has entries to chew on.
        champ = await f.finalized_agent(session, index=1, composite=0.76)
        for i, composite in enumerate((0.71, 0.67, 0.63, 0.59), start=2):
            await f.finalized_agent(session, index=i, composite=composite)

        # Three agents held in ath_pending_review (public: under_review).
        await f.ath_review_agent(session, index=6, original=champ, composite=0.81)

        _, reopened = await f.ath_review_agent(
            session, index=7, original=champ, composite=0.78
        )
        reopened.opened_at = f.days_ago(2)
        reopened.reopened_at = f.hours_ago(6)
        session.add_all(
            [
                AthReviewAction(
                    action_id=f.uuid(f"ath-action:{reopened.review_id}:reject"),
                    review_id=reopened.review_id,
                    action="reject",
                    reason="initial verdict: near-identical minhash overlap",
                    actor=_ACTOR,
                    evidence=_evidence(0.97),
                    created_at=f.days_ago(1),
                ),
                AthReviewAction(
                    action_id=f.uuid(f"ath-action:{reopened.review_id}:reopen"),
                    review_id=reopened.review_id,
                    action="reopen",
                    reason="miner supplied provenance; re-examining source diff",
                    actor=_ACTOR,
                    evidence={"provenance_url": "sim://miner-appeal/7"},
                    created_at=f.hours_ago(6),
                ),
            ]
        )
        await session.flush()

        await f.ath_review_agent(session, index=8, original=champ, composite=0.74)

        # A review resolved 'clear': agent back on the leaderboard (scored).
        cleared = await f.finalized_agent(session, index=9, composite=0.69)
        await _resolved_review(
            session,
            f,
            agent=cleared,
            original=champ,
            resolution="clear",
            reason="manual diff: independent implementation, shared harness only",
            opened_days_ago=3,
            resolved_hours_ago=30,
        )

        # A review resolved 'reject': suspected copy confirmed, agent banned.
        banned = await f.finalized_agent(
            session, index=10, composite=0.83, status=AgentStatus.BANNED
        )
        banned.duplicate_of = champ.agent_id
        banned.review_reason = (
            f"anti-copy hold: near-duplicate of {champ.name} v{champ.version}"
        )
        await _resolved_review(
            session,
            f,
            agent=banned,
            original=champ,
            resolution="reject",
            reason="confirmed copy: renamed identifiers over identical AST",
            opened_days_ago=4,
            resolved_hours_ago=40,
        )

        # Two rejected agents behind resolved-reject quarantines.
        await _rejected_with_disputed_quarantine(
            session,
            f,
            index=11,
            reason_code="policy-network-egress",
            dispute_message=(
                "The flagged egress call is a compile-time feature probe; "
                "no network access happens at runtime. Please re-review."
            ),
            dispute_resolved=False,
        )
        await _rejected_with_disputed_quarantine(
            session,
            f,
            index=12,
            reason_code="policy-dynamic-exec",
            dispute_message=(
                "The dynamic-exec finding points at a vendored test fixture "
                "that is never compiled into the agent binary."
            ),
            dispute_resolved=True,
        )
