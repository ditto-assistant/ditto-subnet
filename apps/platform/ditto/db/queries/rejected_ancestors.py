"""Bounded, artifact-bound rejection citations for reviewer attention only.

Payment coldkeys link the exact paid submissions, without a transitive owner
walk. They are a search hint, not evidence of common control or a violation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import exists, or_, select

from ditto.db.models import (
    Agent,
    AgentStatus,
    AthReview,
    AthReviewAction,
    EvaluationPayment,
    ScreeningQuarantine,
)
from ditto_screening_protocol import SourceReviewFinding

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class RejectedAncestor:
    agent_id: UUID
    sha256: str
    references: tuple[str, ...]
    rejected_at: datetime


async def rejected_ancestors(
    session: AsyncSession, *, candidate: Agent
) -> list[RejectedAncestor]:
    """Read up to three earlier, still-rejected submissions with bound citations."""
    coldkeys = (
        select(EvaluationPayment.miner_coldkey)
        .where(EvaluationPayment.agent_id == candidate.agent_id)
        .correlate(None)
    )
    linked_payment = exists(
        select(EvaluationPayment.agent_id)
        .where(
            EvaluationPayment.agent_id == Agent.agent_id,
            EvaluationPayment.miner_coldkey.in_(coldkeys),
        )
        .correlate(Agent)
    )
    eligible = (
        Agent.agent_id != candidate.agent_id,
        Agent.created_at < candidate.created_at,
        Agent.status == AgentStatus.REJECTED,
        or_(Agent.miner_hotkey == candidate.miner_hotkey, linked_payment),
    )
    ath_rows = (
        await session.execute(
            select(Agent, AthReview, AthReviewAction)
            .join(AthReview, AthReview.agent_id == Agent.agent_id)
            .join(AthReviewAction, AthReviewAction.review_id == AthReview.review_id)
            .where(
                *eligible,
                AthReview.status == "resolved",
                AthReview.resolution == "reject",
                AthReviewAction.action == "reject",
            )
            .order_by(AthReviewAction.created_at.desc(), AthReviewAction.action_id)
            .limit(32)
        )
    ).all()
    result: list[RejectedAncestor] = []
    for agent, review, action in ath_rows:
        batch = action.evidence.get("batch_ruling")
        if not isinstance(batch, dict) or batch.get("action") != "reject":
            continue
        # Historical annotations did not repeat expected_sha256; the immutable
        # review snapshot still binds them to the exact artifact held/rejected.
        digest = batch.get("expected_sha256", review.original_evidence.get("sha256"))
        references = batch.get("evidence_references")
        if digest != agent.sha256 or not isinstance(references, list):
            continue
        citations = tuple(ref for ref in references[:64] if isinstance(ref, str))
        if citations:
            result.append(
                RejectedAncestor(agent.agent_id, digest, citations, action.created_at)
            )

    screening_rows = (
        await session.execute(
            select(Agent, ScreeningQuarantine)
            .join(ScreeningQuarantine, ScreeningQuarantine.agent_id == Agent.agent_id)
            .where(
                *eligible,
                ScreeningQuarantine.status == "resolved",
                ScreeningQuarantine.resolution == "reject",
            )
            .order_by(
                ScreeningQuarantine.created_at.desc(), ScreeningQuarantine.quarantine_id
            )
            .limit(32)
        )
    ).all()
    for agent, quarantine in screening_rows:
        try:
            finding = SourceReviewFinding.model_validate(quarantine.finding)
        except ValidationError:
            continue
        if (
            finding.artifact_sha256 != agent.sha256
            or finding.canonical_digest() != quarantine.finding_digest
        ):
            continue
        references = tuple(f"{item.path}:{item.line}" for item in finding.evidence)
        if references:
            result.append(
                RejectedAncestor(
                    agent.agent_id, agent.sha256, references, quarantine.created_at
                )
            )

    unique: dict[UUID, RejectedAncestor] = {}
    for item in sorted(
        result, key=lambda item: (item.rejected_at, str(item.agent_id)), reverse=True
    ):
        unique.setdefault(item.agent_id, item)
    return list(unique.values())[:3]
