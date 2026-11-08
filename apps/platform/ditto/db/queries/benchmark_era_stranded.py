"""Read-only: scored previous-era submissions the active benchmark era excludes.

When a benchmark rollout activates, the active era's validator queue admits
only the submissions :func:`~ditto.db.queries.benchmark_admission.\
benchmark_admission_predicate` names: new-era uploads, submissions first queued
in the new era, the frozen rescore cohort, adopted carryover rows, and audited
contract refreshes. A submission that finished its previous-era quorum without
making the frozen cohort matches none of them. No active-version dataset is
ever pinned for it and no active-version ticket is issued, so it is absent from
the active leaderboard.

That exclusion is the designed era boundary, not a fault in issuance. This
module only makes the population visible: which ``scored``/``live``
submissions carry previous-era scores, have no active-version score and are
not admitted, and whether the owner is already represented on the new board by
a cohort sibling. It writes nothing and decides nothing; the guarded
operator remedy lives in :mod:`ditto.db.queries.benchmark_era_admission`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import aliased

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ScreeningAttempt,
)
from ditto.db.queries.benchmark_admission import (
    admission_rollout_for_active_version,
    benchmark_admission_predicate,
    validator_queue_admission_predicate,
)
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.scores import SCORING_QUORUM, emission_owner_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Bounds the operator read. The production population after a bump is a few
# hundred rows at most; a larger result is truncated and says so.
MAX_STRANDED_ROWS = 2000

REASON_OWNER_IN_COHORT = "owner_in_cohort"
REASON_OWNER_OUTSIDE_COHORT = "owner_outside_cohort"


@dataclass(frozen=True)
class StrandedEraAgent:
    agent_id: UUID
    agent_name: str
    agent_version: int | None
    miner_hotkey: str
    owner_key: str
    status: str
    submitted_at: datetime
    first_screening_pass_at: datetime | None
    previous_score_count: int
    previous_median_composite: float | None
    previous_finalized: bool
    has_active_dataset: bool
    queue_withdrawn: bool
    cohort_sibling_agent_id: UUID | None
    owner_best_stranded: bool
    reason: str


@dataclass(frozen=True)
class StrandedEraReport:
    active_bench_version: int
    rollout: BenchmarkRollout | None
    total: int
    owner_count: int
    finalized_count: int
    owner_in_cohort_count: int
    owner_outside_cohort_count: int
    truncated: bool
    items: list[StrandedEraAgent]


async def stranded_previous_era_agents(
    session: AsyncSession, *, agent_id: UUID | None = None
) -> StrandedEraReport:
    """Every scored previous-era submission the active era does not admit.

    "Previous era" is the admission rollout's ``from_version``. Rows are
    ordered best previous-era median first. ``owner_best_stranded`` marks the
    owner's highest-median stranded row (one per owner key), which is the
    natural candidate set for any per-owner backfill decision.

    ``agent_id`` narrows the read to one exact submission, which is the guard
    the stranded-admission write uses. ``owner_best_stranded`` is meaningless
    on a narrowed read (every returned row is trivially its owner's best).
    """

    active = await active_bench_version(session)
    rollout = await admission_rollout_for_active_version(session, bench_version=active)
    if rollout is None or rollout.from_version >= active:
        return StrandedEraReport(
            active_bench_version=active,
            rollout=rollout,
            total=0,
            owner_count=0,
            finalized_count=0,
            owner_in_cohort_count=0,
            owner_outside_cohort_count=0,
            truncated=False,
            items=[],
        )

    previous = rollout.from_version
    prev_scores = (
        select(
            Score.agent_id.label("agent_id"),
            func.count(Score.validator_hotkey).label("score_count"),
            func.percentile_cont(0.5)
            .within_group(cast(Score.composite, Float))
            .label("median_composite"),
        )
        .where(Score.bench_version == previous)
        .group_by(Score.agent_id)
        .subquery()
    )
    active_score = (
        select(Score.agent_id)
        .where(Score.agent_id == Agent.agent_id, Score.bench_version == active)
        .correlate(Agent)
        .exists()
    )
    active_dataset = (
        select(BenchmarkDataset.agent_id)
        .where(
            BenchmarkDataset.agent_id == Agent.agent_id,
            BenchmarkDataset.bench_version == active,
        )
        .correlate(Agent)
        .exists()
    )
    payment = aliased(EvaluationPayment)
    owner_key = emission_owner_key(agent=Agent, payment=payment)
    # A cohort sibling is any frozen member with the same emission owner (the
    # payment-time coldkey, hotkey fallback), the same identity the rows are
    # grouped by, so an owner with several hotkeys is still seen as on board.
    sibling = aliased(Agent)
    sibling_payment = aliased(EvaluationPayment)
    cohort_sibling = (
        select(BenchmarkRolloutMember.agent_id)
        .join(sibling, sibling.agent_id == BenchmarkRolloutMember.agent_id)
        .outerjoin(sibling_payment, sibling_payment.agent_id == sibling.agent_id)
        .where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
            emission_owner_key(agent=sibling, payment=sibling_payment) == owner_key,
        )
        .order_by(BenchmarkRolloutMember.position)
        .limit(1)
        .correlate(Agent, payment)
        .scalar_subquery()
    )
    # The earliest pass of any kind: every admission has been build-only since
    # source review was retired (2026-11-07), and a later rebuild never moves
    # the minimum.
    first_pass = (
        select(func.min(ScreeningAttempt.finished_at))
        .where(
            ScreeningAttempt.agent_id == Agent.agent_id,
            ScreeningAttempt.status == "passed",
        )
        .correlate(Agent)
        .scalar_subquery()
    )
    statement = (
        select(
            Agent.agent_id,
            Agent.name,
            Agent.version,
            Agent.miner_hotkey,
            owner_key.label("owner_key"),
            Agent.status,
            Agent.created_at,
            first_pass.label("first_pass"),
            prev_scores.c.score_count,
            prev_scores.c.median_composite,
            active_dataset.label("has_active_dataset"),
            (~validator_queue_admission_predicate(bench_version=active)).label(
                "queue_withdrawn"
            ),
            cohort_sibling.label("cohort_sibling"),
        )
        .join(prev_scores, prev_scores.c.agent_id == Agent.agent_id)
        .outerjoin(payment, payment.agent_id == Agent.agent_id)
        .where(
            Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
            ~active_score,
            ~benchmark_admission_predicate(rollout=rollout, bench_version=active),
        )
        .order_by(
            prev_scores.c.median_composite.desc().nulls_last(),
            Agent.created_at.desc(),
            Agent.agent_id,
        )
        .limit(MAX_STRANDED_ROWS + 1)
    )
    if agent_id is not None:
        statement = statement.where(Agent.agent_id == agent_id)
    rows = (await session.execute(statement)).all()
    truncated = len(rows) > MAX_STRANDED_ROWS
    rows = rows[:MAX_STRANDED_ROWS]

    seen_owners: set[str] = set()
    items: list[StrandedEraAgent] = []
    for row in rows:
        best = row.owner_key not in seen_owners
        seen_owners.add(row.owner_key)
        score_count = int(row.score_count)
        items.append(
            StrandedEraAgent(
                agent_id=row.agent_id,
                agent_name=row.name,
                agent_version=row.version,
                miner_hotkey=row.miner_hotkey,
                owner_key=row.owner_key,
                status=AgentStatus(row.status).value,
                submitted_at=row.created_at,
                first_screening_pass_at=row.first_pass,
                previous_score_count=score_count,
                previous_median_composite=(
                    None
                    if row.median_composite is None
                    else float(row.median_composite)
                ),
                previous_finalized=score_count >= SCORING_QUORUM,
                has_active_dataset=bool(row.has_active_dataset),
                queue_withdrawn=bool(row.queue_withdrawn),
                cohort_sibling_agent_id=row.cohort_sibling,
                owner_best_stranded=best,
                reason=(
                    REASON_OWNER_IN_COHORT
                    if row.cohort_sibling is not None
                    else REASON_OWNER_OUTSIDE_COHORT
                ),
            )
        )
    in_cohort = sum(1 for item in items if item.reason == REASON_OWNER_IN_COHORT)
    return StrandedEraReport(
        active_bench_version=active,
        rollout=rollout,
        total=len(items),
        owner_count=len(seen_owners),
        finalized_count=sum(1 for item in items if item.previous_finalized),
        owner_in_cohort_count=in_cohort,
        owner_outside_cohort_count=len(items) - in_cohort,
        truncated=truncated,
        items=items,
    )


__all__ = [
    "MAX_STRANDED_ROWS",
    "REASON_OWNER_IN_COHORT",
    "REASON_OWNER_OUTSIDE_COHORT",
    "StrandedEraAgent",
    "StrandedEraReport",
    "stranded_previous_era_agents",
]
