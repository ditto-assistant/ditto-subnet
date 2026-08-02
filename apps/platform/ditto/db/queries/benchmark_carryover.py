"""Selection and adoption of stranded previous-generation submissions.

Once a new benchmark version activates, nobody will ever issue the third
prior-version score that a partially-scored prior-generation submission is
waiting on. Those submissions are not delayed, they are **permanently
stranded**: no future event can restore the quorum they are blocked on.

This module decides which of them a rollout adopts, and writes the adoption. Two
invariants are load-bearing:

**Admission and dataset generation are one contract.**
:func:`adopt_carryover_agent` writes the
:class:`~ditto.db.models.BenchmarkRolloutCarryover` row and pins the agent's
desired-version :class:`~ditto.db.models.BenchmarkDataset` in the *same*
transaction, and it is the only writer of that row. Admission keys off the row
(see
:func:`~ditto.db.queries.benchmark_admission.benchmark_admission_predicate`), so
an admitted carryover agent always already has the dataset that
:func:`~ditto.db.queries.tickets.issue_ticket` hard-requires. The converse is
deliberately *not* true: a bare dataset is not admission evidence, so a routine
policy rescreen that regenerates one cannot self-admit a historical submission.

**Retirement wins over adoption.** Retiring a stranded submission (see
:mod:`ditto.db.queries.retirement`) and carrying it over are opposite remedies
for the same rows, so exactly one of them may apply. Adoption writes a
carryover row, which is an admission disjunct and therefore makes the submission
ineligible for retirement; conversely
:func:`stranded_prev_gen_candidates` filters retired rows out, so turning this
policy on can never silently resurrect work an operator has already closed out.
Retirement is the stronger claim because it carries a named actor and a reason;
undoing one is a deliberate operator act, not a side effect of a policy flag.

**Adoption adds no new gate.** In particular it does not introduce a rescreening
requirement. ``issue_ticket`` already refuses to lease an agent whose
``screening_policy_version`` is below :data:`SCREENING_POLICY_VERSION`, so an
agent below it could never run no matter what this module admitted. Filtering on
it here only avoids paying a dataset generation for work that can never be
leased; an operator rescreen brings the agent in on the next convergence pass,
with no carryover-specific action needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, literal, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.queue_policy_settings import PrevGenCarryoverSettings
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ScoreAuditEntry,
)
from ditto.db.queries.audit import benchmark_contract_refresh_event
from ditto.db.queries.benchmark_admission import validator_queue_admission_predicate
from ditto.db.queries.benchmark_rollout import DatasetPin, append_rollout_audit
from ditto.db.queries.retirement import retirement_admission_predicate
from ditto.db.queries.retry_state import classify_agent_retry_states
from ditto.db.queries.scores import SCORING_QUORUM, emission_owner_key

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

EVENT_CARRYOVER_ADOPTED = "carryover_adopted"

# Statuses that mean a newer submission is dead rather than superseding: it
# cannot become the owner's live entry, so it is no evidence that they moved on.
#
# ``quarantined`` is deliberately absent. A quarantine is an operator review
# that can still be released, so a quarantined newer submission is in flight,
# not dead. If it is later rejected, the next convergence pass reconsiders the
# older stranded submission automatically -- this is a loop, not a one-shot.
TERMINALLY_REJECTED_STATUSES = (
    AgentStatus.REJECTED,
    AgentStatus.BANNED,
    AgentStatus.SCREENING_FAILED,
)


@dataclass(frozen=True)
class StrandedCandidate:
    """One previous-generation submission eligible for adoption."""

    agent_id: UUID
    miner_hotkey: str
    owner_key: str
    score_count: int
    created_at: datetime


def _dedupe_owner_key(
    scope: str,
    *,
    agent: type[Agent] | AliasedClass[Agent],
    payment: type[EvaluationPayment] | AliasedClass[EvaluationPayment],
) -> ColumnElement[str]:
    """The identity carryover dedupes on, per operator scope.

    ``coldkey`` delegates to :func:`emission_owner_key` -- the one authority for
    "who a miner is" that the ticket allocator itself uses -- so carryover and
    allocation cannot disagree about owners. ``hotkey`` is the single-branch
    widening of it, and ``none`` still needs a key for the audit column even
    though no suppression is applied.
    """
    if scope == "coldkey":
        return emission_owner_key(agent=agent, payment=payment)
    return literal("hotkey:") + agent.miner_hotkey


async def carryover_agent_ids(
    session: AsyncSession, *, rollout: BenchmarkRollout
) -> list[UUID]:
    """Adopted agents for this rollout, in adoption order."""
    return list(
        await session.scalars(
            select(BenchmarkRolloutCarryover.agent_id)
            .where(BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id)
            .order_by(BenchmarkRolloutCarryover.position)
        )
    )


async def stranded_prev_gen_candidates(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    settings: PrevGenCarryoverSettings,
    now: datetime,
) -> list[StrandedCandidate]:
    """Which stranded prior-era submissions this rollout should adopt next.

    Returns only submissions not already adopted, already deduped and already
    capped, so the caller renders exactly the datasets it will pin. Callers must
    check ``settings.enabled`` first: this function runs queries unconditionally
    and the disabled path must run none.

    An agent is **stranded** when all of these hold:

    * it is still ``evaluating`` (nothing else has finalized or killed it);
    * its accepted prior-era score count is at least ``min_score_count`` and
      below :data:`SCORING_QUORUM` -- at or above quorum it is finalized, so by
      definition not stranded;
    * it was submitted strictly before the rollout started, so it is genuinely
      previous-generation and not something the ``created_at`` admission
      disjunct already covers;
    * no other admission disjunct already covers it -- not a frozen cohort
      member, no version-scoped contract-refresh audit entry for the desired
      version, not already adopted;
    * it is on the current screening policy (see the module docstring: this is
      not a new gate, it mirrors one ``issue_ticket`` already enforces);
    * it has not been withdrawn from either era's validator queue;
    * unless ``include_exhausted``, it has not burned its retry budget.
    """
    payment = aliased(EvaluationPayment)
    owner_key = _dedupe_owner_key(settings.dedupe_scope, agent=Agent, payment=payment)
    prev_score_count = (
        select(func.count(Score.validator_hotkey))
        .where(
            Score.agent_id == Agent.agent_id,
            Score.bench_version == rollout.from_version,
        )
        .correlate(Agent)
        .scalar_subquery()
    )
    already_member = (
        select(BenchmarkRolloutMember.agent_id)
        .where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
            BenchmarkRolloutMember.agent_id == Agent.agent_id,
        )
        .correlate(Agent)
        .exists()
    )
    already_adopted = (
        select(BenchmarkRolloutCarryover.agent_id)
        .where(
            BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id,
            BenchmarkRolloutCarryover.agent_id == Agent.agent_id,
        )
        .correlate(Agent)
        .exists()
    )
    already_refreshed = (
        select(ScoreAuditEntry.agent_id)
        .where(
            ScoreAuditEntry.agent_id == Agent.agent_id,
            ScoreAuditEntry.event
            == benchmark_contract_refresh_event(rollout.desired_version),
        )
        .correlate(Agent)
        .exists()
    )
    statement = (
        select(
            Agent.agent_id,
            Agent.miner_hotkey,
            owner_key.label("owner_key"),
            prev_score_count.label("score_count"),
            Agent.created_at,
        )
        .outerjoin(payment, payment.agent_id == Agent.agent_id)
        .where(
            Agent.status == AgentStatus.EVALUATING,
            Agent.created_at < rollout.created_at,
            Agent.screening_policy_version >= SCREENING_POLICY_VERSION,
            prev_score_count >= settings.min_score_count,
            prev_score_count < SCORING_QUORUM,
            ~already_member,
            ~already_adopted,
            ~already_refreshed,
            # Withdrawal is per (agent, version). Respect it in both eras: the
            # era the submission is stranded in, and the era we would adopt it
            # into. Either withdrawal is the miner asking to be left alone.
            validator_queue_admission_predicate(bench_version=rollout.from_version),
            validator_queue_admission_predicate(bench_version=rollout.desired_version),
            # Retirement is the opposite remedy for these exact rows: an
            # operator has already declared this submission's generation closed,
            # with an actor and a reason on the record. Enabling carryover later
            # must never silently un-retire it, so adoption skips it. Scoped to
            # ``from_version`` because that is the era a retirement names; a
            # retirement can only ever be written against an era older than the
            # active one, so a desired-version row cannot exist.
            retirement_admission_predicate(bench_version=rollout.from_version),
        )
        .order_by(
            # Demonstrated progress first: a 2-of-3 has proven it can run, a
            # never-ticketed 0-of-3 has not. Then FIFO, then a stable tiebreak.
            prev_score_count.desc(),
            Agent.created_at.asc(),
            Agent.agent_id.asc(),
        )
    )
    if settings.dedupe_scope != "none":
        sibling = aliased(Agent)
        sibling_payment = aliased(EvaluationPayment)
        # The miner moved on by their own choice: a strictly newer submission
        # that is still alive supersedes their older stranded work, which should
        # not consume fleet capacity on top of it.
        newer_sibling = (
            select(sibling.agent_id)
            .outerjoin(sibling_payment, sibling_payment.agent_id == sibling.agent_id)
            .where(
                sibling.agent_id != Agent.agent_id,
                sibling.created_at > Agent.created_at,
                sibling.status.not_in(TERMINALLY_REJECTED_STATUSES),
                _dedupe_owner_key(
                    settings.dedupe_scope, agent=sibling, payment=sibling_payment
                )
                == owner_key,
            )
            .correlate(Agent, payment)
            .exists()
        )
        statement = statement.where(~newer_sibling)

    rows = (await session.execute(statement)).all()
    if not rows:
        return []

    adopted = list(
        await session.execute(
            select(
                BenchmarkRolloutCarryover.agent_id,
                BenchmarkRolloutCarryover.frozen_owner_key,
            ).where(BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id)
        )
    )
    remaining = settings.max_agents - len(adopted)
    if remaining <= 0:
        return []
    # An owner already holding an adopted slot does not get a second one. The
    # stored key is used as-is: if an operator widens ``dedupe_scope`` later,
    # the change applies to future adoptions rather than retroactively.
    seen_owners = {owner for _agent_id, owner in adopted}

    excluded: set[UUID] = set()
    if not settings.include_exhausted:
        agents = list(
            await session.scalars(
                select(Agent).where(Agent.agent_id.in_([row.agent_id for row in rows]))
            )
        )
        retry_states = await classify_agent_retry_states(
            session, agents=agents, now=now
        )
        excluded = {
            agent_id
            for agent_id, state in retry_states.items()
            if state.state == "exhausted"
        }

    selected: list[StrandedCandidate] = []
    for row in rows:
        if row.agent_id in excluded:
            continue
        if settings.dedupe_scope != "none":
            if row.owner_key in seen_owners:
                continue
            seen_owners.add(row.owner_key)
        selected.append(
            StrandedCandidate(
                agent_id=row.agent_id,
                miner_hotkey=row.miner_hotkey,
                owner_key=row.owner_key,
                score_count=int(row.score_count),
                created_at=row.created_at,
            )
        )
        if len(selected) == remaining:
            break
    return selected


async def adopt_carryover_agent(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    candidate: StrandedCandidate,
    dataset: DatasetPin,
    now: datetime,
    audit_context: dict[str, Any] | None = None,
) -> bool:
    """Adopt one stranded submission into ``rollout``'s benchmark era.

    The **only** writer of :class:`BenchmarkRolloutCarryover`, and it always
    pins the desired-version dataset in the same transaction. That coupling is
    the whole safety property: the admission predicate keys off this row, so an
    admitted agent can never be missing the dataset ``issue_ticket`` requires.

    Deliberately does not touch any rollout state -- not ``cohort_size``, not
    ``status``, not the activation gate. Carryover rides on a transition; it must
    never be able to delay or re-gate one. Returns ``False`` when the agent is
    already adopted, so concurrent convergence passes are idempotent. The caller
    is expected to already hold the rollout row lock.
    """
    if rollout.status not in ("collecting", "blocked_ineligible"):
        return False
    existing = await session.get(
        BenchmarkRolloutCarryover, (rollout.rollout_id, candidate.agent_id)
    )
    if existing is not None:
        return False
    position = (
        int(
            await session.scalar(
                select(
                    func.coalesce(func.max(BenchmarkRolloutCarryover.position), 0)
                ).where(BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id)
            )
        )
        + 1
    )
    session.add(
        BenchmarkRolloutCarryover(
            rollout_id=rollout.rollout_id,
            agent_id=candidate.agent_id,
            position=position,
            frozen_score_count=candidate.score_count,
            frozen_owner_key=candidate.owner_key,
            created_at=now,
        )
    )
    existing_dataset = await session.get(
        BenchmarkDataset, (candidate.agent_id, rollout.desired_version)
    )
    if existing_dataset is None:
        session.add(
            BenchmarkDataset(
                agent_id=candidate.agent_id,
                bench_version=rollout.desired_version,
                seed=dataset.seed,
                sha256=dataset.sha256,
                run_size=dataset.run_size,
                seed_block=dataset.seed_block,
                seed_block_hash=dataset.seed_block_hash,
                created_at=now,
            )
        )
    elif (
        existing_dataset.seed,
        existing_dataset.sha256,
        existing_dataset.run_size,
        existing_dataset.seed_block,
        existing_dataset.seed_block_hash,
    ) != (
        dataset.seed,
        dataset.sha256,
        dataset.run_size,
        dataset.seed_block,
        dataset.seed_block_hash,
    ):
        raise ValueError("existing benchmark dataset does not match carryover")
    payload: dict[str, Any] = {
        "agent_id": str(candidate.agent_id),
        "position": position,
        "frozen_score_count": candidate.score_count,
        "frozen_owner_key": candidate.owner_key,
        "dataset_seed": dataset.seed,
        "dataset_sha256": dataset.sha256,
        "origin": "automatic",
    }
    if audit_context is not None:
        payload.update(audit_context)
    await append_rollout_audit(
        session, rollout, EVENT_CARRYOVER_ADOPTED, payload, now=now
    )
    await session.flush()
    return True
