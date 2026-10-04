"""Shared SQL predicates and grants for screening retries."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.sql.selectable import ScalarSelect

from ditto.db.models import (
    Agent,
    Score,
    ScreeningAttempt,
    ScreeningRetryOverride,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Review outcomes that end a screen without any verdict on the artifact. Each
# used to park the submission until an operator retried it, so on 2026-10-04
# 10 of 23 new submissions sat in ``screening_failed`` with nothing wrong found.
# Platform now grants the retry itself, a bounded number of times per artifact:
# * ``l2-model-inconclusive``: policy v13 V2 already makes the second complete
#   inconclusive review a terminal reject, so one retry settles it either way.
# * ``l3-*-model-provider-fault``: an L3 stage's provider failed after the
#   worker's own backoff; a fresh review usually completes. The worker prefixes
#   the code with the stage (``l2_review.py``: critic, adjudicator, violation
#   adjudicator, cause disagreement).
# * ``l2-causal-role-incomplete``: the review could not finish its causal proof.
# The per-artifact cap (counted by SHA-256, so a resubmitted copy shares it)
# keeps a hostile archive from looping the fleet: after it,
# the submission parks for an operator exactly as before.
AUTO_REVIEW_RETRY_REASON_CODES: frozenset[str] = frozenset(
    {
        "l2-model-inconclusive",
        "l3-adjudicator-model-provider-fault",
        "l3-critic-model-provider-fault",
        "l3-violation-adjudicator-model-provider-fault",
        "l3-cause-disagreement-model-provider-fault",
        "l2-causal-role-incomplete",
    }
)
AUTO_REVIEW_RETRY_MAX_PER_AGENT = 2
AUTO_REVIEW_RETRY_ACTOR = "platform:auto-review-retry"


def latest_screening_attempt_id() -> ScalarSelect[UUID]:
    """Return the latest attempt id correlated to the current agent."""
    return (
        select(ScreeningAttempt.attempt_id)
        .where(ScreeningAttempt.agent_id == Agent.agent_id)
        .order_by(
            ScreeningAttempt.started_at.desc(),
            ScreeningAttempt.attempt_id.desc(),
        )
        .limit(1)
        .correlate(Agent)
        .scalar_subquery()
    )


def failed_screening_retry_authorized() -> ColumnElement[bool]:
    """Match the exact latest-attempt override required by the claim path."""
    return exists(
        select(ScreeningRetryOverride.override_id).where(
            ScreeningRetryOverride.attempt_id == latest_screening_attempt_id()
        )
    )


async def authorize_automatic_review_retry(
    session: AsyncSession,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
    reason_code: str | None,
    now: datetime,
) -> ScreeningRetryOverride | None:
    """Grant one bounded retry of a review that ended without a verdict.

    Writes the same override an operator's Backroom retry writes, so the claim
    path, audit history and miner-visible state are unchanged. Returns ``None``
    when the code is not eligible, the attempt already has a grant, or the
    artifact has used its automatic retries.
    """
    if reason_code not in AUTO_REVIEW_RETRY_REASON_CODES:
        return None
    existing = await session.scalar(
        select(ScreeningRetryOverride.override_id).where(
            ScreeningRetryOverride.attempt_id == attempt.attempt_id
        )
    )
    if existing is not None:
        return None
    if session.get_bind().dialect.name == "postgresql":
        # Copies of one artifact can finish concurrently; serialize the
        # count-and-grant so they cannot both spend the last retry.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"auto-review-retry:{agent.sha256}", 0)
                )
            )
        )
    used = int(
        await session.scalar(
            select(func.count())
            .select_from(ScreeningRetryOverride)
            .where(
                ScreeningRetryOverride.artifact_sha256 == agent.sha256,
                ScreeningRetryOverride.actor == AUTO_REVIEW_RETRY_ACTOR,
            )
        )
        or 0
    )
    if used >= AUTO_REVIEW_RETRY_MAX_PER_AGENT:
        return None
    score_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Score)
            .where(Score.agent_id == agent.agent_id)
        )
        or 0
    )
    override = ScreeningRetryOverride(
        override_id=uuid4(),
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        artifact_sha256=agent.sha256,
        expected_score_count=score_count,
        reason=(
            f"Automatic retry {used + 1}/{AUTO_REVIEW_RETRY_MAX_PER_AGENT} "
            f"after {reason_code}"
        ),
        actor=AUTO_REVIEW_RETRY_ACTOR,
        created_at=now,
    )
    session.add(override)
    await session.flush()
    return override
