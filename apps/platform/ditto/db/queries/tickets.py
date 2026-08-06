"""Mutations + reads against the ``validator_tickets`` table (the k=3 pool).

A submission (agent) is scored by at most :data:`SCORING_QUORUM` validators. A
ticket is issued on demand to a validator that does not already hold one for the
agent, expires if unscored by its deadline (freeing the slot), and is marked
``scored`` when the validator posts a valid score in time.

Issuance locks the candidate agent row and then recounts its occupied slots in a
fresh statement. Concurrent platform replicas therefore serialize allocation
for a given agent and cannot over-issue a fourth slot. The ``(agent_id,
validator_hotkey)`` primary key separately guarantees a validator can hold only
one ticket per agent. A partial unique index plus a per-validator transaction
lock guarantees one validator cannot hold two live assignments across agents.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import func, select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_REQUESTED,
    get_latest_score_retest_event,
)
from ditto.db.queries.benchmark_admission import (
    activated_rollout_for_version,
    benchmark_admission_predicate,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.lease_liveness import maybe_force_expire_lease
from ditto.db.queries.queue_order import (
    EMISSION_CONTENDER_COUNT,
    MIN_OWNER_CONCURRENT_SUBMISSION_LIMIT,
    OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    PROVISIONAL_CONTENDER_LANE_SIZE,
    SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    eligible_screened_image_predicate,
    owner_capacity_gate,
    owner_live_lease_agent_ids,
    queue_candidate_predicate,
    queue_order_terms,
    quorum_capable_validator_hotkeys,
    resolve_fifo_start_at,
    resolve_owner_linkage,
    selected_owner_agent_id,
)
from ditto.db.queries.score_ranking import (
    rank_submissions,
    resolve_ranking_scores,
)
from ditto.db.queries.scores import (
    SCORING_QUORUM,
    LedgerRow,
    list_eligible_ledger,
)
from ditto.db.queries.similarity_budget import (
    SubmissionSketch,
    live_lease_sketches,
    load_submission_sketches,
)
from ditto.db.queries.similarity_grouping import (
    SimilarityBudgetPolicy,
    SimilarityMatch,
    similar_submissions,
)

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement, Select
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Coerce a DB-read datetime to UTC-aware. The SQLite test path round-trips
    timestamps tz-naive; Postgres preserves the zone. Keeps ``deadline``
    comparisons from mixing naive and aware datetimes."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# Statuses that occupy a slot: an issued (live) or already-scored ticket. An
# expired ticket does not count, so its slot re-opens.
_LIVE_TICKET_STATUSES = (TicketStatus.ISSUED, TicketStatus.SCORED)

# A timed-out artifact must not monopolize one validator. Give each validator
# one base attempt while allowing other validators to make an independent
# attempt immediately. Signed infrastructure failures and audited operator
# actions grant their own bounded retries below.
RETRY_COOLDOWN = timedelta(hours=6)
MAX_ATTEMPTS_PER_VERSION = 1


def ticket_attempt_cap(ticket: ValidatorTicket) -> int:
    """Total leases a validator may spend on this agent+version.

    The base per-version budget, plus audited operator grants, plus
    infrastructure-failure compensation. ``attempt_count`` is compared against
    this everywhere issuance, exhaustion, and natural-retry eligibility are
    decided, so the three surfaces agree on one budget.
    """
    return (
        MAX_ATTEMPTS_PER_VERSION
        + ticket.manual_retry_grants
        + ticket.infra_retry_grants
    )


def retry_budget_spent() -> ColumnElement[bool]:
    """SQL twin of :func:`ticket_attempt_cap`, as a ``ValidatorTicket`` predicate.

    True when this lease has burned every attempt it will ever get, so no
    amount of waiting brings the validator back to this agent. The Python and
    SQL forms must describe the same budget, so both live here and every
    surface -- issuance, the completion-first head check, the desired-era
    backlog gate, owner reachability -- spells the sum exactly once.
    """
    return ValidatorTicket.attempt_count >= (
        MAX_ATTEMPTS_PER_VERSION
        + ValidatorTicket.manual_retry_grants
        + ValidatorTicket.infra_retry_grants
    )


# ``EMISSION_CONTENDER_COUNT`` and ``PROVISIONAL_CONTENDER_LANE_SIZE`` now live
# beside the ordering they parameterize, in
# :mod:`ditto.db.queries.queue_order`, and are re-exported here for the callers
# that already import them from this module.


@dataclass(frozen=True)
class ScoreFloor:
    """One queue floor: the number, and the ledger row it was cut from."""

    row: LedgerRow
    score: float
    """The holder's ``official_composite`` -- the number the board ranks it on,
    which is not in general the raw quorum median on :attr:`row`."""


async def get_score_priority_floor_rows(
    session: AsyncSession, *, bench_version: int | None = None
) -> tuple[ScoreFloor | None, ScoreFloor | None]:
    """Return the fifth- and tenth-place floors with the rows that hold them.

    Both floors are cut in the canonical order
    (:func:`ditto.score_order.rank_submissions`) on the canonical
    score (``official_composite``, the continual-mean estimator), which is the
    same order and the same number the public board's ``rank`` and the
    validator's weight fold read. The floors used to be cut on the raw
    ``composite`` the ledger read happens to return, so "fifth place" named two
    different agents depending on which surface you asked -- and, worse, the
    gate the floor drives claims a submission "cannot reach the emission set"
    while emission-set membership is decided by ``official_composite``.

    The floats alone are still not checkable by a miner, so these return the
    rows: any message that quotes a floor must be able to name the row it came
    from.

    Returns ``(continuation_row, provisional_row)``, each ``None`` when the era
    has fewer eligible agents than the position needs.
    """
    # Scalars only. This read is on the hot allocator path -- every ordinary
    # idle-slot claim reaches it -- and it consumes nothing but ranking fields:
    # neither rank_submissions nor resolve_ranking_scores looks at telemetry,
    # and the one caller that keeps a floor ROW reads only `row.agent_id` to
    # name the holder. Hydrating `details` here detoasted and decoded the full
    # per-case audit blob for every eligible row on every claim (~22KB/row over
    # ~1.2k rows on bench v8) and then discarded it, which is user CPU burned on
    # the single API worker's event loop. `include_fingerprints=False` was
    # already set for exactly this reason; `include_details=False` is its pair.
    eligible = [
        row
        for row in await list_eligible_ledger(
            session,
            include_fingerprints=False,
            include_details=False,
            bench_version=bench_version,
        )
        if row.eligible
    ]
    # Nothing below rank five can move either floor, so an era still filling up
    # never pays for the continual-mean reads.
    if len(eligible) < EMISSION_CONTENDER_COUNT:
        return None, None
    official = await resolve_ranking_scores(
        session, rows=eligible, bench_version=bench_version
    )
    ranked = rank_submissions(eligible, scores=official)

    def floor_at(position: int) -> ScoreFloor | None:
        if len(ranked) < position:
            return None
        row = ranked[position - 1]
        return ScoreFloor(row=row, score=official.get(row.agent_id, row.composite))

    return floor_at(EMISSION_CONTENDER_COUNT), floor_at(PROVISIONAL_CONTENDER_LANE_SIZE)


async def get_score_priority_floors(
    session: AsyncSession, *, bench_version: int | None = None
) -> tuple[float | None, float | None]:
    """Return finalized fifth-place and tenth-place floors for one benchmark era.

    The floats are the holders' ``official_composite`` -- the number the board
    ranks them on -- not the raw quorum median stored on the ledger row.
    """
    continuation, provisional = await get_score_priority_floor_rows(
        session, bench_version=bench_version
    )
    return (
        continuation.score if continuation is not None else None,
        provisional.score if provisional is not None else None,
    )


async def get_score_continuation_floor_row(
    session: AsyncSession, *, bench_version: int | None = None
) -> ScoreFloor | None:
    """Return the fifth-place continuation floor and the row that holds it.

    Same selection as :func:`get_score_continuation_floor`, kept as one call so
    the quoted number and the agent it belongs to can never come from two
    separate reads of a moving ledger.
    """
    continuation, _ = await get_score_priority_floor_rows(
        session, bench_version=bench_version
    )
    return continuation


async def get_score_continuation_floor(
    session: AsyncSession, *, bench_version: int | None = None
) -> float | None:
    """Return the finalized fifth-place score for one benchmark era, if five exist.

    The ledger is already best-agent-per-miner and ordered by the same
    eligibility, composite, age, and UUID rules used for emissions. Provisional
    rows remain visible in that ledger, so filter them before selecting fifth.

    ``bench_version`` pins the era the floor is drawn from. Composites are only
    comparable within one benchmark version, so a floor must never be blended
    across versions: left unset, :func:`list_eligible_ledger` pools the
    canonical version together with an open rollout's desired version, and the
    resulting fifth place belongs to no single era. Callers that compare a
    version-scoped composite against this floor must pass that same version.
    Returns ``None`` when the era does not yet have five eligible agents, which
    correctly disables the floor for a benchmark version still filling up.

    The cut is by ``official_composite`` in the canonical order, so this IS the
    score of the row the public board shows at ``rank: 5`` among finalized
    entries. That is deliberate: the gate this floor drives says a submission
    "cannot reach the emission set", and emission-set membership is decided by
    ``official_composite`` in :func:`ditto.api_server.koth.project_koth`. Cutting
    the floor on the raw quorum median instead was gating on a number nothing
    downstream ranks by. Use :func:`get_score_continuation_floor_row` when the
    number has to be attributed to an agent a miner can look up.
    """
    continuation, _ = await get_score_priority_floors(
        session, bench_version=bench_version
    )
    return continuation


async def get_provisional_contender_floor(
    session: AsyncSession, *, bench_version: int | None = None
) -> float | None:
    """Return the finalized tenth-place score for provisional fast-lane admission.

    A provisional submission is only a likely top-ten contender when its first
    accepted score reaches the current finalized top ten.  Ranking the ten best
    provisional rows against one another is not sufficient: when the whole
    provisional pool is weak, that interpretation starves untouched submissions
    without advancing a plausible leaderboard contender.

    ``None`` means fewer than ten finalized owners exist in this benchmark era,
    so there is not yet a meaningful top-ten boundary and the bounded lane keeps
    its bootstrap behavior.
    """
    _, provisional = await get_score_priority_floors(
        session, bench_version=bench_version
    )
    return provisional


async def expire_overdue_tickets(session: AsyncSession, *, now: datetime) -> int:
    """Flip every overdue ``issued`` ticket to ``expired``; return the count.

    Frees the slots of validators that took a ticket and never scored in time,
    so another validator can pick the agent up. Runs inside the caller's
    transaction. Idempotent: a second call over the same window flips nothing.
    """
    overdue = (
        (
            await session.execute(
                select(ValidatorTicket).where(
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.deadline <= now,
                )
            )
        )
        .scalars()
        .all()
    )
    for ticket in overdue:
        ticket.status = TicketStatus.EXPIRED
        # Cooldown begins at the lease deadline, not whenever a later sweep
        # happens to notice it.
        ticket.retry_after = _as_utc(ticket.deadline) + RETRY_COOLDOWN
    return len(overdue)


async def issue_ticket(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    ttl: timedelta,
    bench_version: int | None = MIN_SCOREABLE_BENCH_VERSION,
    artifact_mode: Literal["legacy", "prefer_screened", "screened_only"] = "legacy",
    validator_running_benchmark: bool = False,
    submitted_at_or_after: datetime | None = None,
    fifo_start_at: datetime | None = None,
    completion_first: bool = False,
    slot_id: str = "slot-0",
    only_agent_ids: Collection[UUID] | None = None,
    owner_concurrent_submission_limit: int = OWNER_CONCURRENT_SUBMISSION_LIMIT_DEFAULT,
    similarity_policy: SimilarityBudgetPolicy | None = None,
    similarity_concurrent_submission_limit: int = (
        SIMILARITY_CONCURRENT_SUBMISSION_LIMIT_DEFAULT
    ),
) -> ValidatorTicket | None:
    """Issue a ticket to ``validator_hotkey`` for the next eligible agent.

    Sweeps overdue tickets first, then picks an ``evaluating`` agent that (a)
    has fewer than :data:`SCORING_QUORUM` live tickets and (b) this validator
    does not already hold a live or scored ticket for. Candidates in the
    bounded set whose first score can reach the finalized top ten comes first.
    Other provisional rows do not outrank untouched submissions merely because
    they already have a score. ``completion_first`` instead makes benchmark-era
    FIFO primary so the oldest submission reaches quorum before the next
    submission is opened. A
    2-of-3 submission that can no longer reach this era's emission set sorts
    behind every other candidate rather than being withheld, so it still
    finalizes once the queue drains. A prior expired row is reissued only after
    its cooldown and only when an infrastructure or operator grant extends its
    budget for the same benchmark version. Returns the
    ticket, or ``None`` when there is no work for this validator ("no job for
    you"). Runs inside the caller's transaction.

    ``only_agent_ids`` narrows the candidate set to an explicit id list and, when
    set, suppresses ``submitted_at_or_after``. The two cannot both apply: the
    caller that names ids has already decided membership, and the arrival-time
    filter exists to express "this era's submissions only" -- which is precisely
    what a previous-generation carryover list is not. Every other eligibility
    rule (screening policy, versioned dataset, rollout admission, per-owner
    serialization, attempt caps) still applies unchanged, so a named agent that
    is not genuinely leasable still yields ``None``. Default ``None`` leaves
    every existing caller's behaviour untouched.

    ``owner_concurrent_submission_limit`` caps how many of one owner's
    submissions may hold live leases at once, and applies **only** after the
    ordinary selection above has come back empty. While any other owner has
    work this validator can take, the owner rails are unchanged: one live
    submission per owner fleet-wide, pinned to one generation. Only once this
    validator has walked its whole candidate set without finding anything does
    the loop re-offer the rows it refused on that ceiling alone -- so a second
    submission from one owner can consume a slot that would otherwise sit idle,
    but can never be served ahead of another owner. ``1`` disables the second
    pass entirely and restores strict serialization; see
    :func:`~ditto.db.queries.queue_order.owner_capacity_gate`.

    ``similarity_policy`` turns on the fairness rail that the owner ceiling
    cannot reach: near-identical submissions share one concurrency budget
    whichever key paid for them, because spreading one submission across many
    coldkeys defeats every ownership-derived rule the platform can verify.
    ``None`` (the default) leaves it off entirely -- no sketch is read and the
    allocator behaves exactly as it did before -- so the whole feature is inert
    until an operator enables it. Unlike the owner ceiling this rail applies on
    **both** passes, ordinary and last-resort: the monopoly it prevents is one
    family filling the fleet precisely when nothing else is leasable, which is
    the last-resort case. It never refuses a submission the owner has already
    started; see the gate's docstring for that exemption and its bound.
    """
    # No row exists to lock before a validator's first claim. Serialize that
    # gap explicitly on Postgres; the unique partial index remains the durable
    # backstop and SQLite test transactions are already single-writer.
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{validator_hotkey}:{slot_id}", 0)
                )
            )
        )
    await expire_overdue_tickets(session, now=now)
    if bench_version is None:
        raise ValueError("benchmark version is required for ticket issuance")
    activated_rollout = await activated_rollout_for_version(
        session, bench_version=bench_version
    )
    if fifo_start_at is None:
        fifo_start_at = await resolve_fifo_start_at(
            session, bench_version=bench_version, rollout=activated_rollout
        )
    contract = benchmark_contract(bench_version)
    requires_screened = (
        contract.requires_screened_image or artifact_mode == "screened_only"
    )

    # A validator slot executes one benchmark at a time. Polling the same slot
    # again (including after a process restart) must resume that still-live
    # lease instead of allocating unrelated work and leaving it stranded.
    eligible_screened_image = eligible_screened_image_predicate(
        bench_version=bench_version
    )
    rollout_admitted = None
    if activated_rollout is not None:
        rollout_admitted = benchmark_admission_predicate(
            rollout=activated_rollout, bench_version=bench_version
        )
    existing_statement = (
        select(ValidatorTicket)
        .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
            ValidatorTicket.purpose_revision > 0,
            ValidatorTicket.deadline > now,
        )
        .order_by(ValidatorTicket.issued_at.asc(), ValidatorTicket.agent_id.asc())
        .limit(1)
        .with_for_update()
    )
    if requires_screened:
        existing_statement = existing_statement.where(eligible_screened_image)
    if rollout_admitted is not None:
        existing_statement = existing_statement.where(rollout_admitted)
    existing = await session.scalar(existing_statement)
    if existing is not None:
        return existing
    incompatible_existing = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .limit(1)
        .with_for_update()
    )
    if incompatible_existing is not None:
        if (
            incompatible_existing.purpose != TicketPurpose.CANONICAL_QUORUM
            or incompatible_existing.purpose_revision <= 0
        ):
            # Continual and deployment-transition leases own this slot until
            # their deadline. A canonical claim must neither serve nor cancel
            # work from another authorization lane.
            return None
        # A validator may only resume a lease from the requested benchmark era
        # and artifact contract. Release an *idle* incompatible assignment so a
        # retired benchmark cannot leak into the active queue after activation --
        # but only on positive, fresh evidence of idleness. ``not
        # validator_running_benchmark`` alone is absence of evidence: it is
        # derived from a stored capacity blob that silently freezes whenever
        # heartbeat ingest fails, and acting on it destroyed three healthy v7
        # runs mid-benchmark. When the lease is not proven idle the slot simply
        # keeps it until its deadline.
        if not await maybe_force_expire_lease(
            session,
            ticket=incompatible_existing,
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            now=now,
            context="issue_ticket",
            running_benchmark_reported=validator_running_benchmark,
            requested_bench_version=bench_version,
        ):
            return None

    # Scoped to the era this ticket is for: a v2 fifth place says nothing about
    # whether a v4 two-score maximum is still in contention.
    score_continuation_floor, provisional_contender_floor = (
        (None, None)
        if completion_first
        else await get_score_priority_floors(session, bench_version=bench_version)
    )

    # Agents this validator must not receive right now: live/scored tickets,
    # same-version tickets cooling down after expiry, and same-version tickets
    # that already consumed the single-attempt base budget. A benchmark-version
    # bump resets the budget so repaired scoring software can revisit the artifact.
    already_mine = select(ValidatorTicket.agent_id).where(
        ValidatorTicket.validator_hotkey == validator_hotkey,
        ValidatorTicket.bench_version == bench_version,
        (
            ValidatorTicket.status.in_(_LIVE_TICKET_STATUSES)
            | (
                (ValidatorTicket.status == TicketStatus.EXPIRED)
                & ((ValidatorTicket.retry_after > now) | retry_budget_spent())
            )
        ),
    )
    # Every lane, tiebreak, and floor comparison below is built by
    # :func:`ditto.db.queries.queue_order.queue_order_terms`, which the public
    # queue preview composes from the same call. Restating the order here is
    # what produced three miner-visible divergences in one evening.
    queue_order = queue_order_terms(
        bench_version=bench_version,
        now=now,
        artifact_mode=artifact_mode,
        fifo_start_at=fifo_start_at,
        score_continuation_floor=score_continuation_floor,
        provisional_contender_floor=provisional_contender_floor,
        completion_first=completion_first,
        validator_hotkey=validator_hotkey,
    )
    # Lock one candidate Agent row before counting its tickets. The recount is a
    # separate statement after the lock is acquired, so under Postgres READ
    # COMMITTED it sees any ticket committed by the previous lock holder.
    # SKIP LOCKED lets unrelated agents continue allocating concurrently.

    # The fleet that could still score this version, read once per issuance.
    # Only used to decide whether a *pinned* owner generation is provably
    # finished; the preview reads the same fleet for the same predicate.
    capable_hotkeys = await quorum_capable_validator_hotkeys(
        session, bench_version=bench_version, now=now
    )
    # Both owner reads are properties of the owner, not of the row, and nothing
    # below writes until a winner is chosen, so they are stable for the whole
    # selection and are answered once per distinct owner rather than once per
    # candidate. That also makes the second pass free.
    owner_facts: dict[tuple[str, ...], tuple[UUID | None, set[UUID]]] = {}
    # The fleet's live-lease sketches, for the same reason and with the same
    # lifetime: a property of the fleet rather than of the row, read at most
    # once per issuance and only when the similarity rail is enabled at all.
    twin_sketches: tuple[SubmissionSketch, ...] | None = None
    skipped: list[UUID] = []
    # Rows this validator was refused purely by the owner ceiling, in the queue
    # order they were refused in. These -- and only these -- are what the
    # last-resort pass reconsiders; a row skipped because the submission is
    # already at quorum, or because this validator cannot contribute to it, is
    # not leasable at any ceiling and must never reappear here.
    owner_capacity_skips: list[UUID] = []
    last_resort = False
    reconsidered: set[UUID] | None = None

    def eligible_candidates() -> Select[tuple[UUID]]:
        """This validator's remaining candidate set, unordered and unlocked.

        The validator-independent half of the filter is shared with the queue
        preview, so "would the allocator even look at this row" has one answer.
        The per-validator half (``already_mine``) and this loop's ``skipped``
        set stay here: neither has a fleet-wide truth value.
        """
        statement = select(Agent.agent_id).where(
            *queue_candidate_predicate(
                bench_version=bench_version,
                artifact_mode=artifact_mode,
                rollout=activated_rollout,
                submitted_at_or_after=(
                    None if only_agent_ids is not None else submitted_at_or_after
                ),
            )
        )
        if not completion_first:
            statement = statement.where(Agent.agent_id.not_in(already_mine))
        if only_agent_ids is not None:
            statement = statement.where(Agent.agent_id.in_(set(only_agent_ids)))
        if skipped:
            statement = statement.where(Agent.agent_id.not_in(skipped))
        return statement

    while True:
        candidate = eligible_candidates()
        if reconsidered is not None:
            candidate = candidate.where(Agent.agent_id.in_(reconsidered))
        candidate = (
            # The ordinary queue first advances the bounded set of strongest
            # scored provisional contenders, one best submission per miner. A
            # stronger 1-of-3 candidate can therefore receive its second score
            # before a weaker 2-of-3 candidate receives its third. The remaining
            # queue gives untouched work a coverage opportunity before weak
            # provisional rows. The fresh lane uses queue_order's FIFO-first
            # alternative.
            candidate.order_by(*queue_order)
            .limit(1)
            .with_for_update(of=Agent, skip_locked=not completion_first)
        )
        agent_id = (await session.execute(candidate)).scalar_one_or_none()
        if agent_id is None:
            if (
                last_resort
                or not owner_capacity_skips
                or owner_concurrent_submission_limit
                <= MIN_OWNER_CONCURRENT_SUBMISSION_LIMIT
            ):
                return None
            # Nothing else in the fleet is leasable by this validator. The loop
            # above did not sample capacity or guess at idleness -- it walked
            # the entire candidate set in queue order and every row that was
            # not refused by the owner ceiling was refused for a reason no
            # ceiling can lift. So the last-resort predicate is not a second
            # query that could disagree with the first; it is the first query
            # coming back empty, which is the only form of "no other owner has
            # eligible work" that cannot be wrong in the optimistic direction.
            #
            # This matters because the obvious implementation -- "is a slot
            # idle right now?" -- is the failure mode
            # ``desired_era_work_outstanding`` already shipped: it truthfully
            # reported drained while 84 rows waited, because a row that is one
            # tick from leasable is not an empty queue. Serving an owner's
            # second submission on that signal would put them ahead of another
            # owner. Serving it only here structurally cannot.
            #
            # With one exception, which is the same mistake wearing a different
            # hat: the selection above runs ``SKIP LOCKED``, so a row another
            # replica is mid-allocation on silently vanishes from it. An empty
            # result can therefore mean "the queue is empty" or "the only rows
            # left are momentarily locked", and relaxing on the second would
            # hand an owner a slot because of a lock. Re-ask without the lock
            # before committing to the fall-through; if anything eligible is
            # merely unavailable this instant, yield the poll and let the
            # validator come back. Conservative in the safe direction: the cost
            # is one skipped poll, and the alternative is a fairness bug that
            # only appears under concurrency.
            if await session.scalar(eligible_candidates().limit(1)) is not None:
                return None
            last_resort = True
            reconsidered = set(owner_capacity_skips)
            # Rows skipped for other reasons are absent from ``reconsidered``
            # anyway; clearing lets the ceiling be re-asked on the rows that
            # are, in the same queue order.
            skipped = []
            continue

        # One paid owner may have many generations, but only one generation may
        # occupy validator capacity at a time. Serialize by the immutable
        # payment-time coldkey (legacy rows fall back to hotkey), then re-check
        # for an issued sibling after taking the lock. Accepted scores are
        # history, not occupied capacity, but they keep the first progressing
        # generation selected until it settles. The post-lock checks close the
        # race where two platform replicas select different agents for the same
        # owner before either ticket exists.
        linkage = await resolve_owner_linkage(session, agent_id=agent_id)
        if session.get_bind().dialect.name == "postgresql":
            # A legacy row inherits every payment-time coldkey previously
            # observed for its hotkey. Lock those identities in sorted order,
            # so it also serializes with a paid generation submitted after a
            # hotkey rotation. A truly unlinked legacy row falls back to its
            # hotkey. The canonical ordering prevents multi-key deadlocks.
            for owner_lock_key in linkage.advisory_lock_keys:
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended(owner_lock_key, 0)
                        )
                    )
                )
        # Both owner rails, in one expression the queue preview calls too, so
        # relaxing the rule cannot relax it for only one of them.
        #
        # ``selected_owner_agent_id`` keeps one current-era generation selected
        # across the gaps between its leases. Otherwise a validator that
        # already scored the selected row can open a sibling after the last
        # lease becomes SCORED, and that new lease diverts every eligible
        # validator away from finishing the first generation. Historical
        # overlaps converge deterministically on the generation whose
        # accepted/live progress began first. Expired-only attempts do not pin
        # an owner, so failed work can still drain.
        owner_key = linkage.advisory_lock_keys
        if owner_key not in owner_facts:
            owner_facts[owner_key] = (
                await selected_owner_agent_id(
                    session,
                    linkage=linkage,
                    bench_version=bench_version,
                    now=now,
                    provisional_contender_floor=provisional_contender_floor,
                    rollout=activated_rollout,
                    capable_validator_hotkeys=capable_hotkeys,
                ),
                await owner_live_lease_agent_ids(session, linkage=linkage, now=now),
            )
        owner_selected_agent_id, owner_live_leases = owner_facts[owner_key]
        # The fairness rail's inputs. Read inside the loop but cached across
        # it: the fleet's live-lease sketches are the same set for every
        # candidate this pass, and the per-candidate sketch is one projection
        # of a column the ticket path otherwise never touches.
        twins: tuple[SimilarityMatch, ...] = ()
        if similarity_policy is not None:
            if twin_sketches is None:
                twin_sketches = await live_lease_sketches(session, now=now)
            candidate_sketch = (
                await load_submission_sketches(session, agent_ids=[agent_id])
            ).get(agent_id)
            if candidate_sketch is not None:
                twins = similar_submissions(
                    candidate_sketch,
                    [sketch for sketch in twin_sketches if sketch.agent_id != agent_id],
                    policy=similarity_policy,
                )
        gate = owner_capacity_gate(
            agent_id=agent_id,
            selected_agent_id=owner_selected_agent_id,
            live_lease_agent_ids=owner_live_leases,
            concurrent_submission_limit=owner_concurrent_submission_limit,
            last_resort=last_resort,
            similar_lease_agent_ids=[twin.agent_id for twin in twins],
            similarity_concurrent_submission_limit=(
                similarity_concurrent_submission_limit
            ),
        )
        if gate is not None:
            if gate == "similarity_serialized":
                # Logged rather than silently skipped, because this is the one
                # refusal whose reason a miner cannot reconstruct from their own
                # submission -- it depends on a row belonging to someone else.
                logger.info(
                    "similarity budget holds agent %s: %s",
                    agent_id,
                    ", ".join(twin.describe() for twin in twins[:3]),
                )
            skipped.append(agent_id)
            if not last_resort and gate == "owner_serialized":
                # Only the owner ceiling is reconsidered on the last-resort
                # pass. The similarity rail applies on both, so re-offering a
                # row it refused would relax it in the one situation it exists
                # for.
                owner_capacity_skips.append(agent_id)
            continue
        occupied = await session.scalar(
            select(func.count()).where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.bench_version == bench_version,
                ValidatorTicket.status.in_(_LIVE_TICKET_STATUSES),
            )
        )
        if (occupied or 0) >= SCORING_QUORUM:
            skipped.append(agent_id)
            continue
        if completion_first:
            # Completion-first admission is global, not per validator slot.
            # Every slot waits on the same FIFO head. Once the row lock is
            # acquired, re-check this validator against fresh committed state.
            # A validator that cannot claim the head -- for any reason, live
            # lease included -- advances to the next FIFO candidate instead of
            # idling behind work it can never take.
            same_validator_blocking_status = await session.scalar(
                select(ValidatorTicket.status)
                .where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.validator_hotkey == validator_hotkey,
                    ValidatorTicket.bench_version == bench_version,
                    (
                        ValidatorTicket.status.in_(_LIVE_TICKET_STATUSES)
                        | (
                            (ValidatorTicket.status == TicketStatus.EXPIRED)
                            & (
                                (ValidatorTicket.retry_after > now)
                                | retry_budget_spent()
                            )
                        )
                    ),
                )
                .limit(1)
            )
            if same_validator_blocking_status is not None:
                # This validator cannot contribute another score to the FIFO
                # head: it already holds a live lease on it, already scored it,
                # is cooling down, or exhausted its retry budget. One ticket per
                # (agent, version, validator) is the composite primary key, so
                # none of those states can be advanced by trying again here --
                # the head's remaining quorum slots belong to OTHER validators
                # either way.
                #
                # The live-lease case used to return no job at all, which was
                # correct while a validator ran one benchmark at a time: there
                # was no sibling slot to serve. Since #433 a validator holds up
                # to ``max_concurrent_slots`` leases, and returning None parked
                # every sibling slot for the full ninety-minute lease of the
                # head. That is fleet capacity idling in front of a queue it is
                # allowed to serve, and it does not bring the head one score
                # closer.
                #
                # Skipping preserves FIFO among the work this validator can
                # actually claim, and the ordinary owner-serialization and
                # quorum-occupancy checks above still bound how much of the
                # queue one validator (or one miner) can hold at once.
                skipped.append(agent_id)
                continue
        break

    ticket = await session.get(
        ValidatorTicket, (agent_id, bench_version, validator_hotkey)
    )
    if ticket is None:
        ticket = ValidatorTicket(
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            status=TicketStatus.ISSUED,
            purpose=TicketPurpose.CANONICAL_QUORUM,
            purpose_revision=1,
            issued_at=now,
            deadline=now + ttl,
            bench_version=bench_version,
            attempt_count=1,
            manual_retry_grants=0,
            retry_after=None,
        )
        session.add(ticket)
    else:
        # The composite PK preserves one validator slot per agent. Reuse the
        # expired row with a fresh lease rather than inserting a duplicate.
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CANONICAL_QUORUM
        ticket.purpose_revision += 1
        ticket.legacy_completion_allowed = False
        ticket.slot_id = slot_id
        ticket.issued_at = now
        ticket.deadline = now + ttl
        ticket.attempt_count += 1
        ticket.retry_after = None
        # A fresh lease has its own silence to account for. Carrying the old
        # stamp over would let the liveness gate weigh the *previous* lease's
        # testimony against this one and revoke it while it is still seeding.
        ticket.first_reported_at = None
    await session.flush()
    return ticket


async def issue_confirmation_ticket(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    now: datetime,
    ttl: timedelta,
    bench_version: int,
    seed: int | None = None,
    dataset_sha256: str | None = None,
    slot_id: str = "slot-0",
) -> ValidatorTicket | None:
    """Reissue this validator's existing quorum slot for top-five maintenance.

    The caller has already proven that ``agent_id`` is the one bounded KOTH
    confirmation target, and that ``slot_id`` is a healthy slot this validator
    is not already using -- the retest lane occupies one execution slot exactly
    like a canonical lease, and the operator slot cap is charged for it at the
    call site. Reusing the existing composite-key row keeps score submission on
    the dedicated append-only endpoint.

    This used to refuse a confirmation whenever the validator held **any** live
    lease, fleet-wide, "so this maintenance run cannot interrupt queue
    scoring". That was correct while a validator ran one benchmark at a time.
    Since #433 a validator holds up to ``max_concurrent_slots`` leases, and the
    fleet-wide test meant the lane could only ever fire on a completely idle
    validator -- so the continual retests an operator had explicitly enabled to
    consume *idle* capacity were the one thing that could never claim it. It is
    the same stale single-slot assumption ``issue_ticket`` already corrected in
    its ``same_validator_blocking_status`` branch. The rail that actually
    matters is per-slot, and it is enforced below: one live lease per
    ``(validator, slot)``, which is also the durable unique partial index.
    """
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{validator_hotkey}:{slot_id}", 0)
                )
            )
        )
    await expire_overdue_tickets(session, now=now)
    # Resume this exact retest wherever it already lives. Looked up before the
    # slot rail so a lease that drifted slots (or predates slot-aware issuance)
    # is handed back rather than being refused as an occupant of its own slot.
    existing_retest = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .limit(1)
        .with_for_update()
    )
    if existing_retest is not None:
        return (
            existing_retest
            if existing_retest.purpose == TicketPurpose.CONTINUAL_RETEST
            and existing_retest.purpose_revision > 0
            and (seed is None or existing_retest.seed == seed)
            and (
                dataset_sha256 is None
                or existing_retest.dataset_sha256 == dataset_sha256
            )
            else None
        )
    # One live lease per execution slot, whatever lane issued it. A canonical
    # benchmark on this slot owns it until its deadline; a retest must take a
    # different slot or wait. This is the rail the fleet-wide check was reaching
    # for, and it is the one the unique partial index enforces durably --
    # without it, stamping a slot on a confirmation ticket would start raising
    # integrity errors the moment the lane was unblocked.
    slot_occupied = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .limit(1)
        .with_for_update()
    )
    if slot_occupied is not None:
        return None

    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None or agent.status not in {AgentStatus.SCORED, AgentStatus.LIVE}:
        return None
    # Confirmation evidence is append-only and never changes the canonical k=3
    # Score rows, so any permitted validator may contribute the shared wave.
    # Restricting this to the original three scorers strands healthy validators
    # and can make a five-member wave impossible to finish during fleet churn.
    latest_retest = await get_latest_score_retest_event(
        session,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
    )
    if (
        latest_retest is not None
        and latest_retest.event == EVENT_SCORE_RETEST_REQUESTED
    ):
        # An operator-authorized canonical replacement owns this validator/agent
        # lifecycle until it is completed or released. Continual maintenance
        # must never repurpose its expired mutable row.
        return None

    ticket = await session.get(
        ValidatorTicket, (agent_id, bench_version, validator_hotkey)
    )
    if ticket is not None and ticket.retry_after is not None:
        retry_after = ticket.retry_after
        if retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=UTC)
        if retry_after > now:
            return None
    if ticket is None:
        ticket = ValidatorTicket(
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            status=TicketStatus.ISSUED,
            purpose=TicketPurpose.CONTINUAL_RETEST,
            purpose_revision=1,
            issued_at=now,
            deadline=now + ttl,
            bench_version=bench_version,
            seed=seed,
            dataset_sha256=dataset_sha256,
            attempt_count=1,
            manual_retry_grants=0,
            retry_after=None,
        )
        session.add(ticket)
    else:
        same_version = ticket.bench_version == bench_version
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CONTINUAL_RETEST
        ticket.purpose_revision += 1
        ticket.legacy_completion_allowed = False
        # The reused row carries whichever slot last leased this (agent,
        # version, validator) pair. Restamp it: the validator binds its
        # execution slot to the ticket's ``slot_id`` (ditto-subnet #270), so a
        # stale value sends every progress report to a slot with no matching
        # lease and the run is discarded at ingest.
        ticket.slot_id = slot_id
        ticket.issued_at = now
        ticket.deadline = now + ttl
        ticket.bench_version = bench_version
        ticket.seed = seed
        ticket.dataset_sha256 = dataset_sha256
        ticket.seed_block = None
        ticket.seed_block_hash = None
        ticket.attempt_count = ticket.attempt_count + 1 if same_version else 1
        ticket.manual_retry_grants = ticket.manual_retry_grants if same_version else 0
        ticket.retry_after = None
    await session.flush()
    return ticket


async def get_open_ticket(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    now: datetime,
    deadline: datetime,
    bench_version: int | None = MIN_SCOREABLE_BENCH_VERSION,
    slot_id: str | None = None,
    for_update: bool = False,
) -> ValidatorTicket | None:
    """Return the validator's live ticket matching the signed lease.

    ``bench_version=None`` is reserved for signed heartbeat progress, where the
    exact lease deadline identifies work across benchmark versions. The
    one-issued-ticket-per-validator index keeps that cross-version lookup
    unambiguous.
    """
    statement = select(ValidatorTicket).where(
        ValidatorTicket.agent_id == agent_id,
        ValidatorTicket.validator_hotkey == validator_hotkey,
        ValidatorTicket.status == TicketStatus.ISSUED,
    )
    if bench_version is not None:
        statement = statement.where(ValidatorTicket.bench_version == bench_version)
    if slot_id is not None:
        statement = statement.where(ValidatorTicket.slot_id == slot_id)
    if for_update:
        statement = statement.with_for_update()
    ticket = await session.scalar(statement)
    if (
        ticket is None
        or _as_utc(ticket.deadline) <= now
        or _as_utc(ticket.deadline) != _as_utc(deadline)
    ):
        return None
    return ticket


async def get_live_slot_ticket(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
    for_update: bool = False,
    skip_locked: bool = False,
) -> ValidatorTicket | None:
    """Return the live lease occupying one validator slot, by identity alone.

    Deliberately *not* deadline-stamped, unlike :func:`get_open_ticket`. This is
    for confirming a heartbeat's per-slot occupancy claim, where an exact
    deadline match is the wrong test: the platform re-issues a lease in place
    (``deadline = now + ttl``), and the validator keeps signing progress with the
    deadline it was handed at claim time. That drift is normal and says nothing
    about whether a run is alive, but matching on it evicts a healthy slot from
    the stored capacity — which blanks its progress in the fleet view and, worse,
    hands the revoker a false idle verdict.

    ``(validator_hotkey, slot_id)`` is unique among issued tickets, so pinning
    the agent as well makes this unambiguous without the deadline. Scoring still
    goes through the exact-deadline path; nothing here accepts a score.
    """
    statement = select(ValidatorTicket).where(
        ValidatorTicket.agent_id == agent_id,
        ValidatorTicket.validator_hotkey == validator_hotkey,
        ValidatorTicket.slot_id == slot_id,
        ValidatorTicket.status == TicketStatus.ISSUED,
    )
    if skip_locked and not for_update:
        raise ValueError("skip_locked requires for_update")
    if for_update:
        statement = statement.with_for_update(skip_locked=skip_locked)
    ticket = await session.scalar(statement)
    if ticket is None or _as_utc(ticket.deadline) <= now:
        return None
    return ticket


async def list_validator_live_leases(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
) -> list[ValidatorTicket]:
    """Return every lease this validator holds, as the ledger sees it.

    This is the *assignment* truth the platform owes a reporter, and it infers
    nothing about validator liveness — it is the exact complement of
    :mod:`ditto.db.queries.lease_liveness`, which infers whether a validator is
    still working a lease. A lease is here iff it is ``issued`` and not yet past
    its deadline, which is the same predicate that lets a score be accepted in
    :func:`get_open_ticket`. Anything revoked, expired, or scored is absent by
    construction, so a reporter comparing its running slots against this list
    learns exactly which of its runs the platform no longer wants.

    Read-only and lock-free on purpose: it must never be able to block, stall,
    or fail the heartbeat write it rides along with.
    """
    rows = await session.scalars(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .order_by(ValidatorTicket.slot_id)
    )
    return list(rows)


async def mark_ticket_scored(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    bench_version: int = MIN_SCOREABLE_BENCH_VERSION,
) -> None:
    """Mark the validator's ticket for the agent ``scored`` (slot spent). No-op
    if there is no ticket row (e.g. a legacy score predating ticketing)."""
    ticket = await session.get(
        ValidatorTicket, (agent_id, bench_version, validator_hotkey)
    )
    if ticket is not None:
        ticket.status = TicketStatus.SCORED
