"""Shared validator-retry classification.

Both the operator triage routes (``endpoints/admin_validation_retry.py``) and the
public operations feed (``endpoints/public.py``) must agree on why a below-quorum
submission is or is not advancing. That verdict lives here once — a pure gate
plus a bulk loader — so the two surfaces can never drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.retry_state import RecommendedRetryAction, RetryState
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    Score,
    SubmissionRetirement,
    ValidatorHeartbeat,
    ValidatorQueueWithdrawal,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto.db.queries.queue_removal import is_in_force, removal_in_force
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.tickets import ticket_attempt_cap

VALIDATOR_RETRY_ONLINE_WINDOW = timedelta(minutes=5)
_UNRESOLVED_ROLLOUT = object()

# Named scorer/validator codes that are the agent's, not the fleet's. A
# submission whose remaining quorum slots all died on these cannot be repaired
# by a fresh lease of the same image, so an operator retry grant is the wrong
# remedy. Fail-closed: anything else (timeout prose, Kaniko identity, silent
# expiry, missing detail, mixed causes) stays on the retry-grant path.
AGENT_ATTRIBUTABLE_FAILURE_DETAILS = frozenset(
    {
        "inference_allowance_exhausted",
        "inference_request_rejected",
        "model_inference_required",
    }
)
AGENT_ATTRIBUTABLE_WITHDRAW_REASON = (
    "exhausted on agent-attributable failures; withdraw rather than retry"
)


async def work_available_validator_hotkeys(
    session: AsyncSession,
    *,
    now: datetime,
    heartbeat_rows: Iterable[ValidatorHeartbeat] | None = None,
) -> set[str]:
    """Validators that can consume an automatic retry right now.

    A remaining attempt on an offline, paused, or erroring validator is ledger
    capacity, not fleet capacity. It must stay preserved for that validator's
    eventual return, but it must not prevent an operator from reviving exhausted
    healthy siblings needed to reach quorum in the meantime.
    """
    if heartbeat_rows is not None:
        cutoff = now - VALIDATOR_RETRY_ONLINE_WINDOW
        return {
            row.validator_hotkey
            for row in heartbeat_rows
            if aware(row.seen_at) >= cutoff and row.state not in ("paused", "error")
        }
    return set(
        (
            await session.scalars(
                select(ValidatorHeartbeat.validator_hotkey).where(
                    ValidatorHeartbeat.seen_at >= now - VALIDATOR_RETRY_ONLINE_WINDOW,
                    ValidatorHeartbeat.state.not_in(("paused", "error")),
                )
            )
        ).all()
    )


def aware(value: datetime) -> datetime:
    """Coerce a DB-read datetime to UTC-aware (SQLite round-trips tz-naive)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def resolve_bench_version(
    *,
    all_tickets: list[ValidatorTicket],
    all_scores: list[Score],
    canonical_version: int,
) -> int:
    """Pick the benchmark era an agent's retry state belongs to.

    Live/expired work is the strongest signal (the era it is being scored on
    right now); otherwise its newest recorded score; otherwise the canonical
    active version for an agent with no ticket or score history yet.
    """
    work_tickets = [
        ticket
        for ticket in all_tickets
        if ticket.status in (TicketStatus.ISSUED, TicketStatus.EXPIRED)
    ]
    if work_tickets:
        return max(
            work_tickets,
            key=lambda ticket: (aware(ticket.issued_at), ticket.bench_version),
        ).bench_version
    if all_scores:
        return max(
            all_scores,
            key=lambda score: (aware(score.generated_at), score.bench_version),
        ).bench_version
    return canonical_version


def is_exhausted(ticket: ValidatorTicket) -> bool:
    """An expired ticket whose validator has spent its whole attempt budget."""
    return (
        ticket.status == TicketStatus.EXPIRED
        and ticket.attempt_count >= ticket_attempt_cap(ticket)
    )


def current_failure_detail(ticket: ValidatorTicket) -> str | None:
    """``failure_detail`` for *this* lease, or ``None`` if it is missing/stale.

    Reissue preserves the last report, so a detail whose ``failed_at`` predates
    ``issued_at`` belongs to a superseded attempt and must not classify the
    current one.
    """
    if ticket.status != TicketStatus.EXPIRED:
        return None
    if ticket.failure_detail is None or ticket.failed_at is None:
        return None
    if aware(ticket.failed_at) < aware(ticket.issued_at):
        return None
    return ticket.failure_detail


def remaining_exhausted_tickets(
    *, scores: list[Score], tickets: list[ValidatorTicket]
) -> list[ValidatorTicket]:
    """Expired, budget-spent tickets that still need to contribute a score."""
    score_hotkeys = {score.validator_hotkey for score in scores}
    return [
        ticket
        for ticket in tickets
        if ticket.status != TicketStatus.SCORED
        and ticket.validator_hotkey not in score_hotkeys
        and is_exhausted(ticket)
    ]


def is_agent_attributable_exhaustion(
    *, scores: list[Score], tickets: list[ValidatorTicket]
) -> bool:
    """Whether every remaining quorum slot died of a named agent failure.

    Fail-closed. A timeout, Kaniko identity error, silent expiry, missing
    detail, live lease, or mixed remaining set must not look like a withdraw
    candidate — those still need an operator retry after a verified infra fix.
    """
    needed = SCORING_QUORUM - len(scores)
    remaining = remaining_exhausted_tickets(scores=scores, tickets=tickets)
    if needed <= 0 or len(remaining) < needed:
        return False
    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return False
    details = [current_failure_detail(ticket) for ticket in remaining]
    return bool(details) and all(
        detail in AGENT_ATTRIBUTABLE_FAILURE_DETAILS for detail in details
    )


def dominant_agent_failure_detail(
    *, scores: list[Score], tickets: list[ValidatorTicket]
) -> str | None:
    """The single agent-attributable code when every remaining slot agrees."""
    remaining = remaining_exhausted_tickets(scores=scores, tickets=tickets)
    details = {current_failure_detail(ticket) for ticket in remaining}
    if len(details) != 1:
        return None
    detail = next(iter(details))
    return detail if detail in AGENT_ATTRIBUTABLE_FAILURE_DETAILS else None


def recommended_retry_action(
    *,
    scores: list[Score],
    tickets: list[ValidatorTicket],
    recovery_allowed: bool,
) -> RecommendedRetryAction | None:
    """Operator next step for a below-quorum row, or ``None`` when none applies."""
    if is_agent_attributable_exhaustion(scores=scores, tickets=tickets):
        return "withdraw"
    if recovery_allowed:
        return "retry"
    return None


def recovery_gate(
    *,
    agent: Agent,
    scores: list[Score],
    tickets: list[ValidatorTicket],
    now: datetime,
    bench_version: int,
    rollout_qualification: bool = False,
    work_available_hotkeys: set[str] | None = None,
) -> tuple[bool, bool, str | None, list[ValidatorTicket]]:
    """Decide whether an automatic or operator retry is possible.

    Returns ``(automatic_retry_available, recovery_allowed, blocking_reason,
    tickets_to_grant)``. ``tickets_to_grant`` is the minimum set of expired,
    budget-exhausted tickets an operator grant would revive to restore quorum.
    """
    score_count = len(scores)
    waiting_for_scores = agent.status == AgentStatus.EVALUATING or (
        rollout_qualification and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
    )
    if not waiting_for_scores:
        return False, False, "submission is not waiting for validator scores", []
    if agent.screening_policy_version < SCREENING_POLICY_VERSION:
        return False, False, "submission is not on the current screening policy", []
    if score_count >= SCORING_QUORUM:
        return False, False, "submission already reached scoring quorum", []
    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return False, False, "a validator ticket is still active", []
    score_hotkeys = {score.validator_hotkey for score in scores}
    non_scored = [
        ticket
        for ticket in tickets
        if ticket.status != TicketStatus.SCORED
        and ticket.validator_hotkey not in score_hotkeys
        and ticket.bench_version == bench_version
    ]
    naturally_retryable = [
        ticket
        for ticket in non_scored
        if ticket.status == TicketStatus.EXPIRED
        and ticket.attempt_count < ticket_attempt_cap(ticket)
        and (
            work_available_hotkeys is None
            or ticket.validator_hotkey in work_available_hotkeys
        )
    ]
    if naturally_retryable:
        available = any(
            ticket.retry_after is None or aware(ticket.retry_after) <= now
            for ticket in naturally_retryable
        )
        reason = (
            "automatic validator retry is already available"
            if available
            else "automatic validator retry is still cooling down"
        )
        return available, False, reason, []

    needed = SCORING_QUORUM - score_count
    exhausted = sorted(
        (ticket for ticket in non_scored if is_exhausted(ticket)),
        key=lambda ticket: (
            0
            if work_available_hotkeys is None
            or ticket.validator_hotkey in work_available_hotkeys
            else 1,
            aware(ticket.deadline),
            ticket.validator_hotkey,
        ),
    )
    if len(exhausted) < needed:
        return False, False, "not enough expired tickets to restore quorum", []
    if is_agent_attributable_exhaustion(scores=scores, tickets=tickets):
        return False, False, AGENT_ATTRIBUTABLE_WITHDRAW_REASON, []
    return False, True, None, exhausted[:needed]


def withdrawal_gate(
    *,
    agent: Agent,
    scores: list[Score],
    tickets: list[ValidatorTicket],
    bench_version: int,
) -> tuple[bool, str | None]:
    """Allow withdrawal only after enough failed slots make quorum impossible.

    Unlike another retry grant, a safe terminal queue withdrawal remains
    available after the manual-recovery limit is spent.
    """

    if agent.status != AgentStatus.EVALUATING:
        return False, "submission is not waiting for validator scores"
    if agent.screening_policy_version < SCREENING_POLICY_VERSION:
        return False, "submission is not on the current screening policy"
    if len(scores) >= SCORING_QUORUM:
        return False, "submission already reached scoring quorum"
    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return False, "a validator ticket is still active"

    scored_hotkeys = {score.validator_hotkey for score in scores}
    needed = SCORING_QUORUM - len(scores)
    exhausted = sum(
        1
        for ticket in tickets
        if ticket.bench_version == bench_version
        and ticket.validator_hotkey not in scored_hotkeys
        and is_exhausted(ticket)
    )
    if exhausted < needed:
        return False, "submission can still reach quorum automatically"
    return True, None


def eviction_closes_era(*, agent: Agent, scores: list[Score]) -> bool:
    """Whether an eviction should also withdraw the submission from this era.

    Evaluating-below-quorum is the original 2026-07-27 escape hatch: revoke the
    live leases *and* close assignment. Continual-retest zombies on scored or
    banned agents need the leases gone without kicking a finalized miner off
    the retest cohort — a withdrawal would 409 the next eviction after the
    lane re-issued a fresh ticket.
    """

    return agent.status == AgentStatus.EVALUATING and len(scores) < SCORING_QUORUM


def eviction_gate(
    *,
    agent: Agent,
    scores: list[Score],
    tickets: list[ValidatorTicket],
) -> tuple[bool, str | None]:
    """Allow an operator eviction of live canonical or continual-retest leases.

    :func:`withdrawal_gate` is a *cleanup* tool. It only accepts a submission
    that has already stopped consuming validator capacity — enough exhausted
    tickets that quorum is unreachable, and no live lease — which means it can
    tidy up a corpse but cannot stop an agent that is actively burning slots.

    That was the entire gap on 2026-07-27: a family of hanging submissions held
    6 of the fleet's 12 validator slots, each attempt running the full 90-minute
    lease and reporting nothing, and the only available answer was to wait for
    each of them to exhaust its own retry budget at a cost of ~4.5
    validator-hours per attempt. Eviction is the answer to that, so it must be
    permitted in exactly the two states withdrawal refuses:

    * ``a validator ticket is still active`` — the live leases are the capacity
      the operator is trying to reclaim, so refusing on their presence refuses
      the whole point;
    * ``submission can still reach quorum automatically`` — "it could still
      finish on its own" is true of every fleet-starving agent, right up until
      it has burned everything.

    The screening-policy check is dropped as well: a submission below the
    current policy is no longer leasable, but it can still be *holding* a lease
    issued before the bump, and that slot is just as stuck.

    A live issued ticket is always evictable, including continual-retest
    leases on scored or banned agents. Evaluating-below-quorum remains
    evictable even with no live ticket so the original era-close path still
    works. Everything else that makes this safe is at the route: an exact
    expected-state snapshot, a written reason, a distinct confirmation phrase,
    a named actor, and one audit row per evicted lease.
    """

    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return True, None
    if agent.status != AgentStatus.EVALUATING:
        return False, (
            "submission is not waiting for validator scores and has no live leases"
        )
    if len(scores) >= SCORING_QUORUM:
        return False, (
            "submission already reached scoring quorum and has no live leases"
        )
    return True, None


def reinstatement_gate(
    *,
    agent: Agent,
    scores: list[Score],
    removal: ValidatorQueueWithdrawal | None,
    bench_version: int,
    active_version: int,
) -> tuple[bool, str | None]:
    """Allow a queue removal to be undone, with no retry budget added.

    :func:`eviction_gate` made eviction reachable while a submission was still
    burning validator slots, which is what made it useful — and one-way, which is
    what kept it unused. An evicted miner had paid an evaluation fee for a
    benchmark era that ended by operator decision, and their only route back was
    a fresh submission and a second fee. That is a punishment, and it should not
    be the price of a capacity decision taken against a submission nobody has
    shown to be malicious.

    Reinstatement is the exact inverse of the queue effect and *nothing else*.
    This applies equally to an eviction and to an exhausted-submission
    withdrawal: both are operator-authored removals of a paid submission, and
    both must be reversible when later evidence justifies another bounded retry.
    It clears the removal so ``validator_queue_admission_predicate``
    admits the agent again; it does not resurrect the revoked leases, reset
    ``attempt_count``, mint a no-fault grant, or forgive a spent operator
    recovery. That inertness is the security property: eviction already refuses
    to compensate (``compensate=False``), so if reinstatement handed back
    headroom the pair would become an attempt printer — evict, reinstate, collect
    — which is precisely the amplifier ``MAX_AGENT_INFRA_RETRY_GRANTS`` (12, per
    agent per era, ditto-platform#522) was introduced to bound. Operator
    recoveries remain explicit, snapshot-guarded actions that grant exactly one
    future lease per selected validator ticket; this route writes none, so an
    evict/reinstate cycle is budget-neutral by construction and no number of
    cycles changes that.

    The refusals:

    * nothing to reverse, or it was reversed already;
    * **the era moved on.** A removal is scoped to the benchmark version it was
      taken in, and no validator will ever be issued a ticket for a closed one,
      so reinstating into it would report success and change nothing. Refused
      explicitly, naming both versions, rather than left as a silent no-op;
    * the submission is no longer waiting for scores, already has quorum, or has
      fallen behind the current screening policy (below it nothing is leasable,
      so re-admission would again be a no-op).
    """

    if removal is None:
        return False, "submission is not removed from this benchmark queue"
    if not is_in_force(removal):
        return False, "removal has already been reinstated"
    if bench_version != active_version:
        return (
            False,
            f"benchmark era v{bench_version} is no longer active "
            f"(the queue is now serving v{active_version})",
        )
    if agent.status != AgentStatus.EVALUATING:
        return False, "submission is not waiting for validator scores"
    if agent.screening_policy_version < SCREENING_POLICY_VERSION:
        return False, "submission is not on the current screening policy"
    if len(scores) >= SCORING_QUORUM:
        return False, "submission already reached scoring quorum"
    return True, None


def classify_retry_state(
    *,
    automatic: bool,
    allowed: bool,
    reason: str | None,
    scores: list[Score],
    tickets: list[ValidatorTicket],
) -> RetryState | None:
    """Fold the gate's verdict into a single triage label.

    Returns ``None`` for a submission that is not actually below quorum and
    therefore not stuck. ``exhausted`` is the only label that needs an operator.
    """
    score_hotkeys = {score.validator_hotkey for score in scores}
    if len(scores) >= SCORING_QUORUM:
        return None
    if any(ticket.status == TicketStatus.ISSUED for ticket in tickets):
        return "running"
    if automatic:
        return "retry_available"
    if reason == "automatic validator retry is still cooling down":
        return "cooling_down"
    if allowed:
        return "exhausted"
    # No natural or operator-grantable retry is pending. Only call it exhausted
    # (needs a human) when enough validators have burned their budget that the
    # submission cannot reach quorum on its own — at least as many exhausted
    # non-scored tickets as scores still needed. Fewer than that means the
    # remaining slots were simply never leased and fresh validators will fill
    # them, so it is still queued, not stuck.
    needed = SCORING_QUORUM - len(scores)
    exhausted_unscored = sum(
        1
        for ticket in tickets
        if is_exhausted(ticket) and ticket.validator_hotkey not in score_hotkeys
    )
    if exhausted_unscored >= needed:
        return "exhausted"
    return "queued"


@dataclass(frozen=True)
class AgentRetryState:
    """One agent's resolved retry classification and the evidence behind it."""

    state: RetryState
    bench_version: int
    score_count: int
    automatic_retry_available: bool
    recovery_allowed: bool
    blocking_reason: str | None
    earliest_retry_after: datetime | None
    scores: list[Score]
    tickets: list[ValidatorTicket]


async def classify_agent_retry_states(
    session: AsyncSession,
    *,
    agents: list[Agent],
    now: datetime,
    require_work_available_validator: bool = False,
    available_validator_hotkeys: set[str] | None = None,
    rollout: BenchmarkRollout | None | object = _UNRESOLVED_ROLLOUT,
    canonical_version: int | None = None,
) -> dict[UUID, AgentRetryState]:
    """Classify a batch of agents' retry state with bounded bulk reads.

    ``EVALUATING`` agents below quorum get an entry. A scored/live agent also
    gets one only while it belongs to the open desired-version rollout cohort:
    its active-era score is settled, but its rollout qualification is still
    below quorum and consumes the same bounded retry budget.
    """
    input_by_id = {agent.agent_id: agent for agent in agents}
    settled_candidates = {
        agent_id: agent
        for agent_id, agent in input_by_id.items()
        if agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
    }
    if rollout is _UNRESOLVED_ROLLOUT:
        rollout = await open_rollout(session) if settled_candidates else None
    assert rollout is None or isinstance(rollout, BenchmarkRollout)
    rollout_member_ids: set[UUID] = set()
    if rollout is not None and settled_candidates:
        rollout_member_ids = set(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id).where(
                    BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                    BenchmarkRolloutMember.agent_id.in_(list(settled_candidates)),
                )
            )
        )
    agent_by_id = {
        agent.agent_id: agent
        for agent in agents
        if agent.status == AgentStatus.EVALUATING
        or (
            agent.agent_id in rollout_member_ids
            and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
        )
    }
    if not agent_by_id:
        return {}
    id_subq = (
        select(Agent.agent_id)
        .where(Agent.agent_id.in_(list(agent_by_id)))
        .scalar_subquery()
    )
    tickets_by_agent: dict[UUID, list[ValidatorTicket]] = {}
    for ticket in (
        await session.scalars(
            select(ValidatorTicket).where(ValidatorTicket.agent_id.in_(id_subq))
        )
    ).all():
        tickets_by_agent.setdefault(ticket.agent_id, []).append(ticket)
    scores_by_agent: dict[UUID, list[Score]] = {}
    for score in (
        await session.scalars(select(Score).where(Score.agent_id.in_(id_subq)))
    ).all():
        scores_by_agent.setdefault(score.agent_id, []).append(score)
    withdrawals = set(
        (
            await session.execute(
                select(
                    ValidatorQueueWithdrawal.agent_id,
                    ValidatorQueueWithdrawal.bench_version,
                ).where(
                    ValidatorQueueWithdrawal.agent_id.in_(id_subq),
                    # Reinstated removals do not count: the submission is back in
                    # the queue and belongs in triage again.
                    removal_in_force(),
                )
            )
        ).all()
    )
    # A retired submission's benchmark generation has closed, so a retry state
    # would be describing attempts denominated in a version no validator will
    # ever be issued a ticket for. Treated exactly like a withdrawal here: no
    # retry classification, which also keeps the row out of the operator
    # "stuck" triage feed and out of the public retry chips.
    retirements = set(
        (
            await session.execute(
                select(
                    SubmissionRetirement.agent_id,
                    SubmissionRetirement.bench_version,
                ).where(SubmissionRetirement.agent_id.in_(id_subq))
            )
        ).all()
    )
    if canonical_version is None:
        canonical_version = await active_bench_version(session)
    available_hotkeys = available_validator_hotkeys
    if available_hotkeys is None and require_work_available_validator:
        available_hotkeys = await work_available_validator_hotkeys(session, now=now)

    result: dict[UUID, AgentRetryState] = {}
    for agent_id, agent in agent_by_id.items():
        all_tickets = sorted(
            tickets_by_agent.get(agent_id, []),
            key=lambda ticket: (aware(ticket.deadline), ticket.validator_hotkey),
        )
        all_scores = scores_by_agent.get(agent_id, [])
        bench_version = resolve_bench_version(
            all_tickets=all_tickets,
            all_scores=all_scores,
            canonical_version=canonical_version,
        )
        if (agent_id, bench_version) in withdrawals:
            continue
        if (agent_id, bench_version) in retirements:
            continue
        v_scores = [s for s in all_scores if s.bench_version == bench_version]
        v_tickets = [t for t in all_tickets if t.bench_version == bench_version]
        automatic, allowed, reason, _ = recovery_gate(
            agent=agent,
            scores=v_scores,
            tickets=v_tickets,
            now=now,
            bench_version=bench_version,
            rollout_qualification=(
                rollout is not None
                and bench_version == rollout.desired_version
                and agent_id in rollout_member_ids
            ),
            work_available_hotkeys=available_hotkeys,
        )
        state = classify_retry_state(
            automatic=automatic,
            allowed=allowed,
            reason=reason,
            scores=v_scores,
            tickets=v_tickets,
        )
        if state is None:
            continue
        scored_hotkeys = {s.validator_hotkey for s in v_scores}
        result[agent_id] = AgentRetryState(
            state=state,
            bench_version=bench_version,
            score_count=len(v_scores),
            automatic_retry_available=automatic,
            recovery_allowed=allowed,
            blocking_reason=reason,
            # Only a ticket that can still retry has a meaningful "retry at"
            # time; an exhausted ticket's stale cooldown must not read as
            # "coming back soon".
            earliest_retry_after=min(
                (
                    aware(t.retry_after)
                    for t in v_tickets
                    if t.status == TicketStatus.EXPIRED
                    and t.retry_after is not None
                    and t.validator_hotkey not in scored_hotkeys
                    and not is_exhausted(t)
                    and (
                        available_hotkeys is None
                        or t.validator_hotkey in available_hotkeys
                    )
                ),
                default=None,
            ),
            scores=v_scores,
            tickets=v_tickets,
        )
    return result


async def is_open_rollout_qualification(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    for_update: bool = False,
) -> bool:
    """Whether this settled agent is still waiting on an open rollout quorum."""

    rollout = await open_rollout(session, for_update=for_update)
    if rollout is None or rollout.desired_version != bench_version:
        return False
    return (
        await session.scalar(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.agent_id == agent_id,
            )
        )
        is not None
    )
