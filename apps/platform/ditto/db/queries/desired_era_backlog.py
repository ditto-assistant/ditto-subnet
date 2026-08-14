"""Is there still desired-era work a validator could take?

Previous-generation work -- the retired era's source backfill and the adopted
carryover of :mod:`ditto.db.queries.benchmark_carryover` -- is the lowest
priority thing the fleet does. Both lanes already sit at the tail of
``request_job``, so a poll only reaches them once every desired-version lane has
returned nothing.

That is not the same as "the desired-era queue is empty", and the difference is
the whole reason this module exists. ``issue_ticket`` answers a *per-validator*
question: one ticket per ``(agent, version, validator)``, an owner may occupy
only one generation at a time, and a validator that already scored an agent, is
cooling down on it, or burned its retry budget is excluded from it. So a
validator can be told "no desired-era work" while the queue is many submissions
deep -- every one of them simply belongs to somebody else's slot. The validator
then leases a retired-era artifact and is gone for the length of that lease,
which is exactly the capacity the desired-era queue was waiting on.

:func:`desired_era_work_outstanding` asks the *fleet-wide* question instead:
does any admitted desired-era submission still have an unfilled quorum slot that
some currently capable validator could take? While the answer is yes, the
previous generation waits.

Deliberately conservative in one direction
==========================================

The predicate reproduces the validator-independent half of ``issue_ticket``'s
candidate filter (status, screening policy, era admission, queue withdrawal,
dataset pin, screened image) and the per-validator exclusions that can never
resolve on their own. It does NOT reproduce owner serialization or the contender
lane, which can defer an agent that this query counts as outstanding.

So the answer errs toward "outstanding". That is the safe direction: the cost of
a false "outstanding" is that retired-era work waits one more poll, while the
cost of a false "drained" is the starvation this gate exists to prevent.

What it must never do is answer "outstanding" forever, or the retired era is
hung out to dry rather than deprioritized. Two exclusions keep it terminating:
an agent whose quorum slots are all filled or leased is not outstanding, and an
agent every capable validator is blocked on -- scored, cooling down, or
retry-exhausted, the state a stranded submission ends in -- is not outstanding
either. A backlog that no validator can act on does not hold the gate shut.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_admission import (
    activated_rollout_for_version,
    benchmark_admission_predicate,
    validator_queue_admission_predicate,
)
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.tickets import retry_budget_spent

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


_UNRESOLVED_ROLLOUT = object()


async def prev_generation_agent_ids(
    session: AsyncSession,
    *,
    bench_version: int,
    agent_ids: Collection[UUID],
    rollout: BenchmarkRollout | None | object = _UNRESOLVED_ROLLOUT,
) -> set[UUID]:
    """Which of ``agent_ids`` can only be served by a previous-generation lane.

    A submission that arrived before the era started is not reachable by any
    ordinary desired-version issuance path -- every one of them filters on
    ``Agent.created_at >= rollout.created_at`` -- so it waits on carryover or
    source backfill, which are strictly last. The frozen rollout cohort is the
    exception: those rows predate the era by construction and are the highest
    priority work there is, since activation waits on them.

    Used to rank the public queue preview honestly. It is a projection helper,
    not an admission gate: it decides display order only.

    The era's rollout row is found whether or not it has finished settling. A
    rollout that has taken ledger authority but is still ``collecting`` its
    remaining qualification is the state this ran in when the mis-ranking was
    reported, and keying only on ``activated`` would have made the fix invisible
    in exactly that state.
    """
    requested = set(agent_ids)
    if not requested:
        return set()
    if rollout is _UNRESOLVED_ROLLOUT:
        rollout = await activated_rollout_for_version(
            session, bench_version=bench_version
        )
    resolved_rollout: Any = cast(Any, rollout)
    if resolved_rollout is None:
        resolved_rollout = await session.scalar(
            select(BenchmarkRollout)
            .where(
                BenchmarkRollout.desired_version == bench_version,
                BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")),
            )
            .order_by(BenchmarkRollout.created_at.desc())
            .limit(1)
        )
    if resolved_rollout is None:
        return set()
    cohort = set(
        await session.scalars(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == resolved_rollout.rollout_id
            )
        )
    )
    return set(
        await session.scalars(
            select(Agent.agent_id).where(
                Agent.agent_id.in_(requested - cohort),
                Agent.created_at < resolved_rollout.created_at,
            )
        )
    )


async def desired_era_work_outstanding(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    now: datetime,
    capable_validator_hotkeys: Collection[str],
) -> bool:
    """Whether any capable validator could still lease desired-era work.

    ``capable_validator_hotkeys`` is the fleet that could actually take the
    work: online validators whose signed heartbeat advertises the desired
    version. An empty fleet means nobody can serve the desired era at all, so
    there is nothing for the retired era to crowd out and the answer is False --
    the gate opens rather than wedging the queue shut on a technicality.
    """
    hotkeys = set(capable_validator_hotkeys)
    if not hotkeys:
        return False

    bench_version = int(rollout.desired_version)
    contract = benchmark_contract(bench_version)

    # Quorum slots this agent has already filled or leased out. At quorum it
    # needs nothing more, so it cannot be waiting on fleet capacity.
    occupied_slots = (
        select(func.count())
        .select_from(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id == Agent.agent_id,
            ValidatorTicket.bench_version == bench_version,
            or_(
                ValidatorTicket.status == TicketStatus.SCORED,
                and_(
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.deadline > now,
                ),
            ),
        )
        .correlate(Agent)
        .scalar_subquery()
    )

    # Capable validators that cannot take this agent right now, for a reason
    # that will not resolve by waiting: they hold or completed its ticket, are
    # inside the retry cooldown, or spent the retry budget. Mirrors
    # ``issue_ticket``'s ``already_mine``, restricted to the capable fleet.
    blocked_validators = (
        select(func.count(func.distinct(ValidatorTicket.validator_hotkey)))
        .where(
            ValidatorTicket.agent_id == Agent.agent_id,
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.validator_hotkey.in_(hotkeys),
            or_(
                ValidatorTicket.status.in_((TicketStatus.ISSUED, TicketStatus.SCORED)),
                and_(
                    ValidatorTicket.status == TicketStatus.EXPIRED,
                    or_(
                        ValidatorTicket.retry_after > now,
                        retry_budget_spent(),
                    ),
                ),
            ),
        )
        .correlate(Agent)
        .scalar_subquery()
    )

    versioned_dataset = (
        select(BenchmarkDataset.agent_id)
        .where(
            BenchmarkDataset.agent_id == Agent.agent_id,
            BenchmarkDataset.bench_version == bench_version,
        )
        .correlate(Agent)
        .exists()
    )

    statement = select(Agent.agent_id).where(
        Agent.status == AgentStatus.EVALUATING,
        Agent.screening_policy_version >= SCREENING_POLICY_VERSION,
        validator_queue_admission_predicate(bench_version=bench_version),
        benchmark_admission_predicate(rollout=rollout, bench_version=bench_version),
        versioned_dataset,
        occupied_slots < SCORING_QUORUM,
        blocked_validators < len(hotkeys),
    )
    if contract.requires_screened_image:
        statement = statement.where(
            Agent.screened_image_sha256.is_not(None),
            Agent.screened_image_size_bytes.is_not(None),
            Agent.screened_image_id.is_not(None),
            Agent.screened_image_ref.is_not(None),
            Agent.screened_image_upload_id.is_not(None),
            Agent.screened_image_verified_at.is_not(None),
            Agent.screening_policy_version >= contract.minimum_screening_policy_version,
        )
    return await session.scalar(statement.limit(1)) is not None
