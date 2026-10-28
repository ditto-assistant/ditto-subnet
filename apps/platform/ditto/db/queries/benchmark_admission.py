"""One authoritative admission boundary for an activated benchmark era."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ColumnElement, case, or_, select
from sqlalchemy.orm.util import AliasedClass

from ditto.api_models.ticket_status import TicketPurpose
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    Score,
    ScoreAuditEntry,
    ScreeningAttempt,
    ScreeningQuarantine,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.audit import benchmark_contract_refresh_event
from ditto.db.queries.queue_removal import removal_in_force

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def activated_rollout_for_version(
    session: AsyncSession, *, bench_version: int
) -> BenchmarkRollout | None:
    """Return the newest activated rollout that established ``bench_version``."""

    return await session.scalar(
        select(BenchmarkRollout)
        .where(
            BenchmarkRollout.desired_version == bench_version,
            BenchmarkRollout.status == "activated",
        )
        .order_by(BenchmarkRollout.activated_at.desc())
        .limit(1)
    )


async def admission_rollout_for_active_version(
    session: AsyncSession, *, bench_version: int
) -> BenchmarkRollout | None:
    """Return the rollout that bounds admission to the active benchmark era.

    Ledger authority can move to the desired version before the wider rescore
    cohort finishes.  During that bounded settling window the rollout remains
    ``collecting``, even though callers already resolved ``bench_version`` as
    the active version.  Looking only for a terminal ``activated`` row drops
    the era predicate and makes every unwithdrawn historical submission look
    admitted to the public queue.

    Ticket issuance deliberately keeps using
    :func:`activated_rollout_for_version`; this helper is only for projections
    and operator admission checks whose input is the already-resolved active
    version.
    """

    return await session.scalar(
        select(BenchmarkRollout)
        .where(
            BenchmarkRollout.desired_version == bench_version,
            BenchmarkRollout.status.in_(
                ("collecting", "blocked_ineligible", "activated")
            ),
        )
        # An authority-holding open rollout is the current boundary.  The
        # activated fallback covers the steady state after it closes.  Keep
        # this one query: Activity is an eight-second polling hot path with a
        # five-round-trip budget enforced by its large-fixture test.
        .order_by(
            case(
                (
                    BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")),
                    0,
                ),
                else_=1,
            ),
            BenchmarkRollout.created_at.desc(),
        )
        .limit(1)
    )


# Ticket purposes that consume a benchmark era's scoring queue. A canary or a
# generator-round panel run is not a quorum attempt on the submission, so it is
# not evidence that the submission was ever served by that era's queue.
_QUEUE_TICKET_PURPOSES = (
    TicketPurpose.CANONICAL_QUORUM,
    TicketPurpose.LEGACY_UNCLASSIFIED,
)


def first_queued_in_era_predicate(
    *,
    rollout: BenchmarkRollout,
    agent: type[Agent] | AliasedClass[Agent] = Agent,
) -> ColumnElement[bool]:
    """Whether a submission first reached a validator queue in ``rollout``'s era.

    Upload time (``created_at``) is not when a submission joins the queue:
    screening runs first, and an infrastructure failure, an operator retry or a
    quarantine review can hold a submission in screening across a rollout. Such
    a submission was never in the previous era's queue, and no previous-era
    lane can reach it after activation: the cohort was frozen from scored work,
    and carryover only adopts ``evaluating`` work below quorum while the rollout
    is still open. Without this disjunct it is stranded for good.

    The rule needs positive evidence of a first queue entry at or after the
    rollout opened, and no evidence of an earlier one:

    * a non-build-only screening pass, or a quarantine released by an operator,
      at or after ``rollout.created_at`` -- the two transitions that move a
      submission into ``evaluating``;
    * no screening pass (build-only included) and no release before it;
    * no previous-era queue ticket or accepted score.

    While the rollout is still open the active era serves such a submission
    like any other in-flight work; this disjunct matters from activation on,
    when that era stops issuing. Pre-activation tickets it collects in the
    previous era make it previous-era work again (third bullet).

    This keeps every exclusion the era boundary exists for. A historical
    submission that was scored, or that sat in the previous era's queue, has an
    earlier pass or release, so a later policy rescreen cannot admit it -- the
    reason a regenerated dataset is not admission evidence. A partially scored
    previous-era submission has previous-era tickets and stays the carryover
    policy's decision. It does not reach rejected, banned or superseded work
    either: issuance still requires ``evaluating``, the current screening
    policy, and the owner and queue rails.
    """

    era_start = rollout.created_at
    passed = ScreeningAttempt.status == "passed"
    released = ScreeningQuarantine.resolution == "release"
    passed_in_era = (
        select(ScreeningAttempt.attempt_id)
        .where(
            ScreeningAttempt.agent_id == agent.agent_id,
            passed,
            ScreeningAttempt.build_only.is_(False),
            ScreeningAttempt.finished_at >= era_start,
        )
        .correlate(agent)
        .exists()
    )
    released_in_era = (
        select(ScreeningQuarantine.quarantine_id)
        .where(
            ScreeningQuarantine.agent_id == agent.agent_id,
            released,
            ScreeningQuarantine.resolved_at >= era_start,
        )
        .correlate(agent)
        .exists()
    )
    passed_before = (
        select(ScreeningAttempt.attempt_id)
        .where(
            ScreeningAttempt.agent_id == agent.agent_id,
            passed,
            ScreeningAttempt.finished_at < era_start,
        )
        .correlate(agent)
        .exists()
    )
    released_before = (
        select(ScreeningQuarantine.quarantine_id)
        .where(
            ScreeningQuarantine.agent_id == agent.agent_id,
            released,
            ScreeningQuarantine.resolved_at < era_start,
        )
        .correlate(agent)
        .exists()
    )
    previous_era_ticket = (
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.agent_id == agent.agent_id,
            ValidatorTicket.bench_version < rollout.desired_version,
            ValidatorTicket.purpose.in_(_QUEUE_TICKET_PURPOSES),
        )
        .correlate(agent)
        .exists()
    )
    previous_era_score = (
        select(Score.agent_id)
        .where(
            Score.agent_id == agent.agent_id,
            Score.bench_version < rollout.desired_version,
        )
        .correlate(agent)
        .exists()
    )
    return (
        (passed_in_era | released_in_era)
        & ~passed_before
        & ~released_before
        & ~previous_era_ticket
        & ~previous_era_score
    )


def benchmark_admission_predicate(
    *,
    rollout: BenchmarkRollout,
    bench_version: int,
    agent: type[Agent] | AliasedClass[Agent] = Agent,
) -> ColumnElement[bool]:
    """SQL predicate for agents allowed to consume this benchmark's capacity.

    A generated dataset is deliberately not admission evidence: routine policy
    rescreens can regenerate one for historical submissions. Only submissions
    received in the new era, submissions whose first queue entry came in the
    new era (see :func:`first_queued_in_era_predicate`), frozen rollout
    members, adopted previous-generation carryover rows, and submissions
    carrying a version-scoped audited benchmark-contract refresh may enter the
    active queue. An ordinary retry grant is not a benchmark-era admission
    credential.

    The carryover disjunct keys off the :class:`BenchmarkRolloutCarryover` ROW,
    never off the existence of a desired-version dataset, so it does not weaken
    the rule above. A carryover row is only ever inserted in the same
    transaction that pins that dataset, which makes admission and generation
    inseparable in the one direction that matters -- admission can never outrun
    generation -- while a routine policy rescreen still creates no carryover row
    and therefore still cannot self-admit a historical submission.
    """

    rollout_member = (
        select(BenchmarkRolloutMember.agent_id)
        .where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
            BenchmarkRolloutMember.agent_id == agent.agent_id,
        )
        .correlate(agent)
        .exists()
    )
    carryover = (
        select(BenchmarkRolloutCarryover.agent_id)
        .where(
            BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id,
            BenchmarkRolloutCarryover.agent_id == agent.agent_id,
        )
        .correlate(agent)
        .exists()
    )
    contract_refresh = (
        select(ScoreAuditEntry.agent_id)
        .where(
            ScoreAuditEntry.agent_id == agent.agent_id,
            ScoreAuditEntry.event == benchmark_contract_refresh_event(bench_version),
        )
        .correlate(agent)
        .exists()
    )
    refresh_retry_grant = (
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.agent_id == agent.agent_id,
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.manual_retry_grants > 0,
        )
        .correlate(agent)
        .exists()
    )
    return or_(
        agent.created_at >= rollout.created_at,
        first_queued_in_era_predicate(rollout=rollout, agent=agent),
        rollout_member,
        carryover,
        contract_refresh & refresh_retry_grant,
    )


async def admitted_agent_ids(
    session: AsyncSession,
    *,
    bench_version: int,
    agent_ids: Sequence[UUID],
) -> set[UUID]:
    """Return which requested agents are admitted to the active-era queue."""

    requested = set(agent_ids)
    if not requested:
        return set()
    rollout = await admission_rollout_for_active_version(
        session, bench_version=bench_version
    )
    statement = select(Agent.agent_id).where(
        Agent.agent_id.in_(requested),
        validator_queue_admission_predicate(bench_version=bench_version),
    )
    if rollout is not None:
        statement = statement.where(
            benchmark_admission_predicate(rollout=rollout, bench_version=bench_version)
        )
    return set(await session.scalars(statement))


def validator_queue_admission_predicate(
    *,
    bench_version: int,
    agent: type[Agent] | AliasedClass[Agent] = Agent,
) -> ColumnElement[bool]:
    """Whether an agent is not withdrawn from this benchmark's queue."""

    withdrawn = (
        select(ValidatorQueueWithdrawal.agent_id)
        .where(
            ValidatorQueueWithdrawal.agent_id == agent.agent_id,
            ValidatorQueueWithdrawal.bench_version == bench_version,
            # A reinstated removal is not a removal. This is the predicate that
            # actually returns an evicted submission to validator assignment.
            removal_in_force(),
        )
        .correlate(agent)
        .exists()
    )
    return ~withdrawn


async def agent_is_admitted(
    session: AsyncSession, *, bench_version: int, agent_id: UUID
) -> bool:
    """Whether one exact agent may consume the benchmark's validator capacity."""

    return agent_id in await admitted_agent_ids(
        session, bench_version=bench_version, agent_ids=[agent_id]
    )
