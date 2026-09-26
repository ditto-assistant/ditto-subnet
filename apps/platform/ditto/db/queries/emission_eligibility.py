"""Reads and append-only writes for the terminal-review emission gate.

Two responsibilities, kept apart on purpose:

* the **posture** (``emission_eligibility_settings_revisions``), read exactly
  like ``burn_settings``;
* the **review facts** for a set of agents (:func:`load_review_postures`), read
  from the tables that already own them -- ``ath_reviews``,
  ``ath_copy_court_recommendations``, ``screening_attempts`` and
  ``screening_quarantines``. Nothing is duplicated into a new state column, so
  there remains exactly one definition of "this artifact's review is open" and a
  withheld verdict always joins back to the row that caused it.

The verdict itself is a pure function in
:mod:`ditto.api_server.emission_eligibility`; this module only fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.db.models import (
    AthCopyCourtRecommendation,
    AthReview,
    EmissionEligibilitySettingsRevision,
    EmissionEligibilityShadowRecord,
    ScreeningAttempt,
    ScreeningQuarantine,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"

DEFAULT_REVIEW_KIND = "copy"
"""``algorithm_provenance.review_kind`` postdates the holds it describes, so a
missing or unrecognized value reads as ``copy`` -- the same fallback
``admin_copy_review._review_kind_filter`` applies. Treating it as "unknown"
instead would let the oldest holds fall out of every filter silently."""


@dataclass(frozen=True)
class AgentReviewPosture:
    """Everything the gate needs about one exact artifact's review.

    Every field is echoed from an existing table; none of it is derived here.
    """

    agent_id: UUID
    review_status: str | None = None
    """``ath_reviews.status``: ``pending`` / ``resolved``, or ``None`` when this
    artifact was never held."""
    review_resolution: str | None = None
    """``ath_reviews.resolution``: ``clear`` / ``reject``."""
    review_resolved_at: datetime | None = None
    review_kind: str | None = None
    """``ath_reviews.algorithm_provenance->>'review_kind'``."""
    court_verdict: str | None = None
    """Newest ``ath_copy_court_recommendations.verdict`` for the hold
    (``clear`` / ``reject`` / ``escalate``). Advisory by construction -- the
    court never resolves a hold by itself -- but an ``escalate`` is the
    platform's own statement that this artifact needs an operator."""
    latest_attempt_status: str | None = None
    latest_attempt_reason_code: str | None = None
    """Newest ``screening_attempts`` row for this artifact."""
    passed_attempt_count: int = 0
    """How many attempts reached ``passed``. Zero means no completed review is
    on record for this exact artifact."""
    active_quarantine_reason_code: str | None = None
    """``screening_quarantines.reason_code`` of the single active row, if any."""


# ─── Posture ────────────────────────────────────────────────────────────────


async def latest_eligibility_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> EmissionEligibilitySettingsRevision | None:
    return await session.scalar(
        select(EmissionEligibilitySettingsRevision)
        .where(EmissionEligibilitySettingsRevision.scope == scope)
        .order_by(EmissionEligibilitySettingsRevision.revision.desc())
        .limit(1)
    )


async def list_eligibility_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[EmissionEligibilitySettingsRevision]:
    return list(
        await session.scalars(
            select(EmissionEligibilitySettingsRevision)
            .order_by(EmissionEligibilitySettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_eligibility_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> EmissionEligibilitySettingsRevision:
    row = EmissionEligibilitySettingsRevision(
        parent_revision=parent_revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


# ─── Review facts ───────────────────────────────────────────────────────────


async def load_review_postures(
    session: AsyncSession, agent_ids: Collection[UUID]
) -> dict[UUID, AgentReviewPosture]:
    """Review facts for every requested artifact, keyed by agent id.

    Four bounded reads rather than one join: the caller's id set is the ledger
    (one row per attested owner, so tens of rows in production), and keeping the
    reads separate means none of them can turn the hot validator ledger path
    into a plan over the whole ``screening_attempts`` table.

    An agent with no row anywhere comes back as a default
    :class:`AgentReviewPosture`, which the classifier reads as "never held,
    never reviewed" -- the common case, and eligible under the default posture.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}

    postures: dict[UUID, dict] = {agent_id: {} for agent_id in ids}

    reviews = list(
        await session.scalars(select(AthReview).where(AthReview.agent_id.in_(ids)))
    )
    review_to_agent: dict[UUID, UUID] = {}
    for review in reviews:
        provenance = review.algorithm_provenance
        kind = provenance.get("review_kind") if isinstance(provenance, dict) else None
        postures[review.agent_id].update(
            review_status=review.status,
            review_resolution=review.resolution,
            review_resolved_at=review.resolved_at,
            review_kind=kind if isinstance(kind, str) and kind else DEFAULT_REVIEW_KIND,
        )
        review_to_agent[review.review_id] = review.agent_id

    if review_to_agent:
        # Newest recommendation per hold. `(review_id, settings_revision)` is
        # unique, so ordering by created_at and keeping the first is the
        # court's current opinion under its current posture.
        recommendations = await session.execute(
            select(
                AthCopyCourtRecommendation.review_id,
                AthCopyCourtRecommendation.verdict,
            )
            .where(AthCopyCourtRecommendation.review_id.in_(review_to_agent))
            .order_by(
                AthCopyCourtRecommendation.review_id,
                AthCopyCourtRecommendation.created_at.desc(),
                AthCopyCourtRecommendation.recommendation_id.desc(),
            )
        )
        seen: set[UUID] = set()
        for review_id, verdict in recommendations:
            if review_id in seen:
                continue
            seen.add(review_id)
            postures[review_to_agent[review_id]]["court_verdict"] = verdict

    latest_attempt_rank = (
        func.row_number()
        .over(
            partition_by=ScreeningAttempt.agent_id,
            order_by=(
                ScreeningAttempt.started_at.desc(),
                ScreeningAttempt.attempt_id.desc(),
            ),
        )
        .label("rank")
    )
    latest_attempts = (
        select(
            ScreeningAttempt.agent_id,
            ScreeningAttempt.status,
            ScreeningAttempt.reason_code,
            latest_attempt_rank,
        )
        .where(ScreeningAttempt.agent_id.in_(ids))
        .subquery()
    )
    for agent_id, status, reason_code in await session.execute(
        select(
            latest_attempts.c.agent_id,
            latest_attempts.c.status,
            latest_attempts.c.reason_code,
        ).where(latest_attempts.c.rank == 1)
    ):
        postures[agent_id].update(
            latest_attempt_status=status, latest_attempt_reason_code=reason_code
        )

    for agent_id, passed in await session.execute(
        select(ScreeningAttempt.agent_id, func.count())
        .where(
            ScreeningAttempt.agent_id.in_(ids),
            ScreeningAttempt.status == "passed",
        )
        .group_by(ScreeningAttempt.agent_id)
    ):
        postures[agent_id]["passed_attempt_count"] = int(passed)

    for agent_id, reason_code in await session.execute(
        select(ScreeningQuarantine.agent_id, ScreeningQuarantine.reason_code).where(
            ScreeningQuarantine.agent_id.in_(ids),
            ScreeningQuarantine.status == "active",
        )
    ):
        postures[agent_id]["active_quarantine_reason_code"] = reason_code

    return {
        agent_id: AgentReviewPosture(agent_id=agent_id, **fields)
        for agent_id, fields in postures.items()
    }


# ─── Shadow rehearsal ledger ────────────────────────────────────────────────


async def record_shadow_exclusions(
    session: AsyncSession,
    *,
    rows: Sequence[dict],
) -> int:
    """Append the withheld set for one window, idempotently.

    ``ON CONFLICT DO NOTHING`` against
    ``emission_eligibility_shadow_window_key``, because every validator poll in
    the window re-evaluates the same set: the first writer records the finding
    and the rest are no-ops. Returns the number of rows actually inserted, which
    is what the caller logs -- a nonzero count is a *new* finding in this
    window, not just another poll.
    """
    if not rows:
        return 0
    result = await session.execute(
        pg_insert(EmissionEligibilityShadowRecord)
        .values(list(rows))
        .on_conflict_do_nothing(constraint="emission_eligibility_shadow_window_key")
        .returning(EmissionEligibilityShadowRecord.record_id)
    )
    return len(result.fetchall())


async def list_shadow_records(
    session: AsyncSession,
    *,
    agent_id: UUID | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> Sequence[EmissionEligibilityShadowRecord]:
    statement = select(EmissionEligibilityShadowRecord)
    if agent_id is not None:
        statement = statement.where(
            EmissionEligibilityShadowRecord.agent_id == agent_id
        )
    if since is not None:
        statement = statement.where(
            EmissionEligibilityShadowRecord.window_start >= since
        )
    return list(
        await session.scalars(
            statement.order_by(
                EmissionEligibilityShadowRecord.created_at.desc(),
                EmissionEligibilityShadowRecord.record_id.desc(),
            ).limit(limit)
        )
    )


async def count_shadow_records_in_window(
    session: AsyncSession, *, window_start: datetime
) -> int:
    """Distinct artifacts recorded as withheld in one window.

    The number an operator reads before flipping ``shadow`` to ``enforce``: it
    is exactly how many rows would leave the fold.
    """
    return int(
        await session.scalar(
            select(
                func.count(func.distinct(EmissionEligibilityShadowRecord.agent_id))
            ).where(EmissionEligibilityShadowRecord.window_start == window_start)
        )
        or 0
    )
