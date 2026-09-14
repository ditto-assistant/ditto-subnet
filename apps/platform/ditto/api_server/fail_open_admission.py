"""Fail-closed handling of admissions the automated court never reviewed.

Between 2026-08-30 and 2026-09-07 the screener's L4 court settled every
refusal (crash, timeout, exhausted budget, malformed verdict) as a clear with
the ``no_proven_breach_before_deadline`` clause. Those rows were admitted and
scored without any layer having read the source. The worker no longer emits
that clause, but the platform must not let a row it carries collect emissions
until an operator has looked: such an agent is held in ``ath_pending_review``
under the deferred-source-review kind, which the eligible ledger already
excludes, and a resolved operator ``clear`` restores it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.deferred_source_review import DEFERRED_REVIEW_KIND
from ditto.db.models import Agent, AthReview, ScreeningQuarantine

FAIL_OPEN_CLEAR_CLAUSE = "no_proven_breach_before_deadline"
# Rows settled before the evidence carried ``clear_clause`` only say it in prose.
FAIL_OPEN_LEGACY_MARKER = "no-proven-breach-before-deadline"
FAIL_OPEN_REVIEW_REASON = (
    "Automated review did not complete; held for operator source review"
)
FAIL_OPEN_ALGORITHM_VERSION = "fail-open-admission-v1"
BACKFILL_ACTOR = "platform:fail-open-backfill"
ADJUDICATED_CLEAR_REASON_CODE = "adjudicated-source-review-clear"


def adjudication_is_fail_open(
    *, decision: str | None, clear_clause: str | None, reason: str | None
) -> bool:
    """True when a clear was the court's fallback rather than its verdict."""
    if decision != "clear":
        return False
    if clear_clause == FAIL_OPEN_CLEAR_CLAUSE:
        return True
    return bool(reason) and FAIL_OPEN_LEGACY_MARKER in (reason or "")


def evidence_marks_fail_open(evidence: Sequence[Any] | None) -> bool:
    """Read a stored quarantine evidence list for the fail-open adjudication row."""
    if not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict) or item.get("module_id") != "adjudication":
            continue
        code = str(item.get("code") or "")
        if adjudication_is_fail_open(
            decision=code.removeprefix("adjudicated-source-review-") or None,
            clear_clause=(
                str(item["clear_clause"]) if item.get("clear_clause") else None
            ),
            reason=str(item.get("summary") or ""),
        ):
            return True
    return False


async def latest_fail_open_admission(
    session: AsyncSession, *, agent_id: UUID
) -> ScreeningQuarantine | None:
    """Return the newest admitting court record for the agent if it was fail-open.

    A later certified clear or reject supersedes an earlier fail-open row, so
    only the newest adjudicated record decides.
    """
    row = await session.scalar(
        select(ScreeningQuarantine)
        .where(
            ScreeningQuarantine.agent_id == agent_id,
            ScreeningQuarantine.reason_code.like("adjudicated-source-review-%"),
        )
        .order_by(ScreeningQuarantine.created_at.desc())
        .limit(1)
    )
    if row is None or row.reason_code != ADJUDICATED_CLEAR_REASON_CODE:
        return None
    return row if evidence_marks_fail_open(row.evidence) else None


async def operator_cleared_since(
    session: AsyncSession, *, agent_id: UUID, since: datetime
) -> bool:
    """True when a human (not the platform) cleared this UUID after ``since``."""
    rows = await session.scalars(
        select(AthReview).where(
            AthReview.agent_id == agent_id,
            AthReview.status == "resolved",
            AthReview.resolution == "clear",
        )
    )
    for review in rows:
        resolved_at = review.resolved_at
        if resolved_at is None:
            continue
        if resolved_at.tzinfo is None:
            resolved_at = resolved_at.replace(tzinfo=since.tzinfo)
        actor = review.resolved_by or ""
        if resolved_at >= since and not actor.startswith("platform:"):
            return True
    return False


async def pending_review(session: AsyncSession, *, agent_id: UUID) -> AthReview | None:
    return await session.scalar(
        select(AthReview).where(
            AthReview.agent_id == agent_id, AthReview.status == "pending"
        )
    )


async def hold_fail_open_admission(
    session: AsyncSession,
    agent: Agent,
    *,
    admission: ScreeningQuarantine,
    now: datetime,
    actor: str,
    source: str,
    score_count: int,
) -> AthReview | None:
    """Move a scored/live agent admitted fail-open into an operator hold.

    Returns the opened review, or ``None`` when the agent is not eligible for
    the hold (wrong status, already held, or an operator cleared it after the
    admitting attempt). Never bans, never touches scores.
    """
    if agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE):
        return None
    if await pending_review(session, agent_id=agent.agent_id) is not None:
        return None
    if await operator_cleared_since(
        session, agent_id=agent.agent_id, since=admission.created_at
    ):
        return None
    previous_status = agent.status.value
    review = AthReview(
        review_id=uuid4(),
        agent_id=agent.agent_id,
        status="pending",
        opened_at=now,
        original_reason=FAIL_OPEN_REVIEW_REASON,
        original_policy_version=agent.screening_policy_version,
        original_evidence={
            "sha256": agent.sha256,
            "score_count": score_count,
            "previous_status": previous_status,
            "admitting_attempt_id": str(admission.attempt_id),
            "admitting_quarantine_id": str(admission.quarantine_id),
            "admitted_at": admission.created_at.isoformat(),
            "clear_clause": FAIL_OPEN_CLEAR_CLAUSE,
        },
        algorithm_provenance={
            "snapshot": "fail-open-admission",
            "review_kind": DEFERRED_REVIEW_KIND,
            "algorithm_version": FAIL_OPEN_ALGORITHM_VERSION,
            "opened_by": actor,
            "backfilled": actor == BACKFILL_ACTOR,
            "opened_at_source": source,
        },
    )
    agent.status = AgentStatus.ATH_PENDING_REVIEW
    agent.review_reason = FAIL_OPEN_REVIEW_REASON
    # The review row is the audit record: opens are not AthReviewAction rows
    # (the action check constraint admits only reopen/clear/reject), and the
    # provenance names the actor and marks the backfill.
    session.add(review)
    return review
