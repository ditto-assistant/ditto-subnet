"""Precautionary ATH hold withdrawal, distinct from policy CLEAR and REJECT.

Withdrawing a manual hold restores the pre-hold score and rank presentation.
Reward eligibility stays on the terminal exact-artifact review gate from
issue #2041. That gate is not available in this tree, so a withdrawal fails
closed on emissions instead of becoming an implicit clearance. A sibling
artifact's ruling is never consulted.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import Agent, AthReview

MANUAL_HOLD_SNAPSHOT = "manual-admin-hold"
WITHDRAW_CONFIRMATION = "WITHDRAW ATH HOLD"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Identified(Protocol):
    agent_id: UUID


_RowT = TypeVar("_RowT", bound=_Identified)


@dataclass(frozen=True)
class WithdrawalRewardDecision:
    """Emission outcome for one exact agent UUID and artifact digest."""

    reward_eligible: bool
    gate: Literal["unavailable"]
    reason: str


def is_manual_precautionary_hold(provenance: dict | None) -> bool:
    """True only for an operator-opened manual hold, not an automated gate."""
    return provenance is not None and provenance.get("snapshot") == MANUAL_HOLD_SNAPSHOT


def withdrawal_reward_decision(
    *,
    agent_id: UUID,
    artifact_sha256: str,
) -> WithdrawalRewardDecision:
    """Reward eligibility after this exact artifact's hold is withdrawn.

    ``agent_id`` and ``artifact_sha256`` identify the artifact the decision
    is about. The function does not load another agent, so a sibling clear
    or reject cannot transfer. Until #2041's gate can return a terminal
    decision for this pair, the answer is ineligible.
    """
    if not isinstance(agent_id, UUID) or _SHA256.fullmatch(artifact_sha256) is None:
        return WithdrawalRewardDecision(
            reward_eligible=False,
            gate="unavailable",
            reason=(
                "Exact artifact identity is missing. Emission eligibility stays closed."
            ),
        )
    return WithdrawalRewardDecision(
        reward_eligible=False,
        gate="unavailable",
        reason=(
            "Hold withdrawn without a misconduct finding or completed "
            "certification. Emission eligibility stays closed until this "
            "exact artifact has a terminal review decision."
        ),
    )


def without_emission_withheld(
    rows: Sequence[_RowT], withheld: set[UUID]
) -> list[_RowT]:
    """Drop artifacts whose withdrawn hold is not reward-eligible."""
    if not withheld:
        return list(rows)
    return [row for row in rows if row.agent_id not in withheld]


async def emission_withheld_agent_ids(
    session: AsyncSession, agent_ids: Iterable[UUID]
) -> set[UUID]:
    """Agents whose own withdrawn hold must stay out of the emission pool.

    Only ``resolution = 'withdraw'`` on the given agent ids qualifies. A
    sibling row's clear, reject, or still-pending review is ignored.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return set()
    rows = (
        await session.execute(
            select(AthReview.agent_id, Agent.sha256)
            .join(Agent, Agent.agent_id == AthReview.agent_id)
            .where(
                AthReview.agent_id.in_(ids),
                AthReview.resolution == "withdraw",
            )
        )
    ).all()
    withheld: set[UUID] = set()
    for agent_id, artifact_sha256 in rows:
        decision = withdrawal_reward_decision(
            agent_id=agent_id,
            artifact_sha256=str(artifact_sha256),
        )
        if not decision.reward_eligible:
            withheld.add(agent_id)
    return withheld
