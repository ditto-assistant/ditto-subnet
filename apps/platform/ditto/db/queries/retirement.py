"""Terminal close-out for submissions whose benchmark generation has ended.

A benchmark rollout is forward-only. Once a newer version activates, no
validator will ever issue a ticket against the older one again, so a submission
still waiting for scores in the closed era is not slow, it is **finished** --
it just has no state that says so. It keeps sitting in the miner-facing
"Waiting for scores" column, holding a ``validator_queue_rank`` it can never
consume, above genuinely current work.

This module supplies the one authority for who qualifies. The rule is
deliberately narrow, because getting it wrong retires work a miner is still
owed:

1. the submission is still ``evaluating`` -- nothing has finalized or killed it;
2. its own benchmark era is strictly older than the active one;
3. it has **not** been admitted to the active era by any of the admission
   disjuncts in :func:`~ditto.db.queries.benchmark_admission
   .benchmark_admission_predicate` -- so a frozen rollout-cohort member, an
   adopted previous-generation carryover row, or an audited contract refresh all
   protect it;
4. it is below quorum in its own era, so no finalized score is being disturbed;
5. it is not already withdrawn from that era's queue, and not already retired.

Clause 3 is what makes retirement and previous-generation **carryover** (the
opposite remedy for the same rows, see
:mod:`ditto.db.queries.benchmark_carryover`) safe to hold in one codebase. The
two can never both apply to a submission:

* carryover **before** retirement -- adoption writes a
  :class:`~ditto.db.models.BenchmarkRolloutCarryover` row, which makes clause 3
  false, so the submission stops being retirement-eligible;
* retirement **before** carryover -- :func:`retirement_admission_predicate` is
  applied by ``stranded_prev_gen_candidates``, so an adoption pass skips retired
  rows. Enabling carryover later can never silently resurrect them.

Retirement therefore takes precedence, on purpose: it is an explicit operator
statement with an actor and a reason, and undoing one should be equally
explicit rather than a side effect of flipping a policy flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm.util import AliasedClass

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    Score,
    SubmissionRetirement,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.queue_removal import removal_in_force
from ditto.db.queries.scores import SCORING_QUORUM

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# What an operator must type to confirm a retirement. Spelled out in full so it
# cannot be produced by an accidental repeat of another confirmation string.
RETIREMENT_CONFIRMATION = "RETIRE PREVIOUS GENERATION"

RetirementPopulation = Literal["never_scored", "partially_scored", "finalized"]

#: Human labels for the populations below. These exist so the preview can show
#: an operator that "previous generation" is not one group but three, and that
#: only two of them are ever eligible.
POPULATION_LABELS: dict[RetirementPopulation, str] = {
    "never_scored": "never scored in its own generation",
    "partially_scored": "scored but never reached quorum",
    "finalized": "reached quorum in its own generation",
}


def classify_population(score_count: int) -> RetirementPopulation:
    """Which previous-generation group a submission belongs to.

    ``finalized`` submissions already hold a complete quorum. They are not
    retirement-eligible and never appear in the waiting column: they carry a
    real score. The distinction matters because "these were scored previously,
    they had a chance" describes ``finalized`` work, while the rows actually
    clogging the queue are ``partially_scored`` and ``never_scored``.
    """
    if score_count >= SCORING_QUORUM:
        return "finalized"
    if score_count > 0:
        return "partially_scored"
    return "never_scored"


def retirement_admission_predicate(
    *,
    bench_version: int,
    agent: type[Agent] | AliasedClass[Agent] = Agent,
) -> ColumnElement[bool]:
    """Whether an agent has not been retired out of this benchmark's queue.

    Shaped exactly like
    :func:`~ditto.db.queries.benchmark_admission.validator_queue_admission_predicate`
    so callers can apply the two side by side.
    """
    retired = (
        select(SubmissionRetirement.agent_id)
        .where(
            SubmissionRetirement.agent_id == agent.agent_id,
            SubmissionRetirement.bench_version == bench_version,
        )
        .correlate(agent)
        .exists()
    )
    return ~retired


@dataclass(frozen=True)
class RetirementVerdict:
    """Why one submission may or may not be retired."""

    allowed: bool
    reason: str | None
    population: RetirementPopulation


def retirement_gate(
    *,
    agent: Agent,
    scores: list[Score],
    tickets: list[ValidatorTicket],
    bench_version: int,
    active_version: int,
    admitted_to_active_era: bool,
    already_retired: bool,
    already_withdrawn: bool,
) -> RetirementVerdict:
    """Decide whether a submission's benchmark generation has closed under it.

    Pure, like :func:`~ditto.db.queries.retry_state.withdrawal_gate`, so the
    preview and the apply path cannot drift.

    Note what is deliberately *absent*: retry budget. A submission with attempts
    left is still retirable, because those attempts are denominated in a version
    nobody will ever issue a ticket for. An operator batch retry that "restores
    slots" on a previous-generation row hands back an allowance that cannot be
    spent, which is exactly the confusion this state exists to end.
    """
    population = classify_population(len(scores))
    if agent.status != AgentStatus.EVALUATING:
        return RetirementVerdict(
            False, "submission is not waiting for validator scores", population
        )
    if bench_version >= active_version:
        return RetirementVerdict(
            False, "submission is queued against the active benchmark", population
        )
    if admitted_to_active_era:
        return RetirementVerdict(
            False, "submission is admitted to the active benchmark", population
        )
    if len(scores) >= SCORING_QUORUM:
        return RetirementVerdict(
            False, "submission already reached scoring quorum", population
        )
    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return RetirementVerdict(
            False, "a validator ticket is still active", population
        )
    if already_withdrawn:
        return RetirementVerdict(
            False, "submission is already removed from this benchmark queue", population
        )
    if already_retired:
        return RetirementVerdict(False, "submission is already retired", population)
    return RetirementVerdict(True, None, population)


async def load_retirement(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    for_update: bool,
) -> SubmissionRetirement | None:
    """The retirement row for one (agent, benchmark era), if any."""
    statement = select(SubmissionRetirement).where(
        SubmissionRetirement.agent_id == agent_id,
        SubmissionRetirement.bench_version == bench_version,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def retired_agent_ids(
    session: AsyncSession,
    *,
    bench_version: int | None = None,
    agent_ids: set[UUID] | None = None,
) -> set[UUID]:
    """Every retired agent, optionally scoped to one benchmark era.

    The public projection needs this as a set: it is read once per request and
    the retired population is a bounded backlog, not a stream.
    """
    statement = select(SubmissionRetirement.agent_id)
    if bench_version is not None:
        statement = statement.where(SubmissionRetirement.bench_version == bench_version)
    if agent_ids is not None:
        if not agent_ids:
            return set()
        statement = statement.where(SubmissionRetirement.agent_id.in_(agent_ids))
    return set(await session.scalars(statement))


async def retirement_count(session: AsyncSession) -> int:
    """How many submissions have been retired, across every benchmark era."""
    return int(
        await session.scalar(select(func.count()).select_from(SubmissionRetirement))
        or 0
    )


async def finalized_prev_gen_count(
    session: AsyncSession, *, active_version: int
) -> int:
    """How many previous-generation submissions already hold a full quorum.

    Reported by the preview purely so an operator cannot confuse two groups.
    These submissions were scored, are on the leaderboard history, never appear
    in the waiting column, and this action does not touch a single one of them.
    A request phrased as "retire the ones that were already scored" is
    describing *this* number, not the eligible set.
    """
    per_agent = (
        select(Score.agent_id)
        .where(Score.bench_version < active_version)
        .group_by(Score.agent_id, Score.bench_version)
        .having(func.count(Score.validator_hotkey) >= SCORING_QUORUM)
        .subquery()
    )
    return int(
        await session.scalar(
            select(func.count(func.distinct(Agent.agent_id)))
            .select_from(Agent)
            .where(
                Agent.agent_id.in_(select(per_agent.c.agent_id)),
                Agent.status.in_(
                    (AgentStatus.SCORED, AgentStatus.LIVE, AgentStatus.EVALUATING)
                ),
            )
        )
        or 0
    )


async def load_withdrawal_versions(
    session: AsyncSession, *, agent_id: UUID
) -> set[int]:
    """Which benchmark eras this agent is currently withdrawn from.

    A reinstated removal is excluded: the submission is back in that era's queue,
    so retirement — which refuses an already-withdrawn era as redundant — is a
    live option for it again.
    """
    return set(
        await session.scalars(
            select(ValidatorQueueWithdrawal.bench_version).where(
                ValidatorQueueWithdrawal.agent_id == agent_id,
                removal_in_force(),
            )
        )
    )
