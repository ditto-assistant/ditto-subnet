"""Durable, version-separated benchmark activation state machine."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import exists, func, select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_contract import (
    benchmark_contract,
    latest_benchmark_contract,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator_capabilities import (
    ScorerBenchmarkCapability,
    ValidatorCapabilities,
    ValidatorStackIdentity,
)
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutAudit,
    BenchmarkRolloutCarryover,
    BenchmarkRolloutMember,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    Score,
    ValidatorHeartbeat,
    ValidatorTicket,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select


# The version a rollout starts FROM when no rollout has ever activated. This
# moves forward as benchmarks activate.
DEFAULT_BENCH_VERSION = 2
# What a version-less report from a pre-bench_version validator actually ran.
# This is a statement about history and is frozen at 2 forever: it must NOT
# follow DEFAULT_BENCH_VERSION, or a future rollout-from bump would silently
# reinterpret every legacy submission as a newer benchmark.
LEGACY_BENCH_VERSION = 2
# The oldest benchmark era that may still produce a score. v2-v6 are RETIRED:
# their datasets, harness contract and model relay are gone, no quorum will ever
# be assembled on them again, and nothing in the ledger consumes their scores.
#
# This is a FLOOR, not a mirror of the active version. It is raised by hand when
# an era is retired for good, never automatically on activation -- a rollout is
# open precisely so ``from_version`` keeps scoring while the fleet migrates, and
# pinning this to ``active_bench_version()`` would kill that lane mid-transition.
#
# Enforced in the database (``scores_bench_version_floor``,
# ``confirmation_scores_bench_version_floor``, ``benchmark_rollout_desired_floor``
# and the ``validator_tickets_bench_version_floor`` insert trigger), so an
# application bug cannot write beneath it. Keep this constant and the migration
# in step: the database is the authority, this is the value the API rejects with.
MIN_SCOREABLE_BENCH_VERSION = 7
# Compatibility name for callers/tests that need the newest *shipped* contract.
# This is discovery metadata only: it no longer opens or selects a rollout.
CANARY_BENCH_VERSION = latest_benchmark_contract().version
# Two fused meanings, deliberately kept in one constant only as a FLOOR:
#
# 1. The KOTH top-five (``rolling_top_five``) -- a consensus quantity that must
#    keep matching ``MIN_DESIRED_AUTHORITY_AGENTS``. Not operator policy.
# 2. Historically also the rollout ACTIVATION GATE: how many top inherited
#    positions must hold a target-version quorum before the rollout may take
#    ledger authority.
#
# Meaning (2) is now operator policy, frozen per rollout onto
# ``BenchmarkRollout.priority_cohort_target``. Read that column, never this
# constant, when asking how wide an existing rollout's gate is. This constant
# survives as meaning (1) and as the configurable floor for meaning (2).
PRIORITY_COHORT_SIZE = 5
# How many inherited agents a new benchmark transition rescores when no operator
# revision has ever been written. This is only the DEFAULT: the effective size
# is the operator policy in ``queue_policy_settings_revisions``, read once
# at rollout start and frozen onto ``BenchmarkRollout.rescore_cohort_target``.
#
# Never read this constant to decide how large an EXISTING rollout should grow
# -- read ``rollout.rescore_cohort_target``. A rollout that froze ten members
# must stay a ten-member rollout even after the operator widens the policy to
# twenty-five, or an in-flight cohort would silently grow underneath the
# validators already scoring it.
DEFAULT_RESCORE_COHORT_SIZE = 10
# The storage ceiling, mirrored by the ``benchmark_rollout_bounded_members`` and
# ``benchmark_rollout_bounded_rescore_target`` CHECK constraints. It is also the
# upper bound an operator may configure. Older 11-25-member rollout snapshots
# predate the top-ten default; they remain readable and an in-flight rollout
# created by an older deployment still finishes without member deletion.
MAX_PERSISTED_RESCORE_COHORT_SIZE = 25
SCORING_QUORUM = 3
# Bench v9's hosted embedding path was not actually executable until the scorer
# release containing #610. Older scorers advertised v9 from the dataset and
# grading contract alone, so trusting ``supported_bench_versions`` would route
# v9 work to a sidecar that rejects every embedding request. This is a semantic
# capability floor, not the active release version: v8 remains compatible and
# every later scorer release continues to satisfy it.
_V9_MINIMUM_SCORER_VERSION = (0, 51, 3)
# How many agents must hold a COMPLETE, ranked desired-version quorum before
# the desired version may take over. Two gates enforce it against the same count
# (``ditto.db.queries.scores.count_ranked_quorum_agents``): the ledger's
# authority switch (``list_eligible_ledger``) and rollout activation
# (``maybe_activate_rollout``), which is where the ledger gate stops applying.
#
# Derived, not guessed: it is exactly the size of the KOTH emission set — the
# champion plus the participation tail. Below that count, flipping the ledger to
# the desired version would drop agents that have no desired-version quorum yet,
# and the fold would have fewer recipients than the emission split expects, so
# emissions would go sparse mid-rollout. Deriving it from KOTH_TAIL_SIZE keeps
# the two from drifting apart if the tail is ever resized.
#
# The value is ``1 (champion) + KOTH_TAIL_SIZE`` from ``ditto.api_server.koth``
# (which mirrors the frozen consensus constants of ditto-subnet's
# ``ditto/validator/weights.py`` / ``config.py``). It is spelled out rather than
# imported because ``ditto.api_server`` imports this module, so importing back
# from it here is a cycle; ``test_min_desired_authority_matches_koth_recipients``
# asserts the equality, so resizing the tail without resizing this fails CI.
MIN_DESIRED_AUTHORITY_AGENTS = 5


class RolloutConflictError(RuntimeError):
    """Raised when a rollout cannot be opened because another one is open.

    ``benchmark_rollouts_one_open_idx`` enforces this in the database; catching
    the condition first turns an opaque IntegrityError 500 into a clean 409.
    """


@dataclass(frozen=True)
class RolloutSnapshotMember:
    agent_id: UUID
    miner_hotkey: str
    composite: float


@dataclass(frozen=True)
class DatasetPin:
    seed: int
    sha256: str
    run_size: str
    seed_block: int | None = None
    seed_block_hash: str | None = None


@dataclass(frozen=True)
class InferenceActivationRequirements:
    """Live process state required before an inference-backed era activates."""

    enabled: bool
    provider_key_configured: bool
    model: str
    routing_mode: Literal["aggregate_throughput", "adaptive"]
    reviewed_manifest_sha256: str | None
    aggregate_provider: str | None = None
    aggregate_profile_revision: str | None = None
    aggregate_calibration_samples: int | None = None
    route_observation_max_age: timedelta = timedelta(minutes=5)


_INFERENCE_ACTIVATION_REQUIREMENTS_SESSION_KEY = (
    "ditto_inference_activation_requirements"
)


def bind_inference_activation_requirements(
    session: AsyncSession,
    requirements: InferenceActivationRequirements | None,
) -> None:
    """Bind the live process readiness snapshot to every authority read."""
    session.info[_INFERENCE_ACTIVATION_REQUIREMENTS_SESSION_KEY] = requirements


async def inference_activation_ready(
    session: AsyncSession,
    *,
    bench_version: int,
    now: datetime,
    requirements: InferenceActivationRequirements | None,
) -> bool:
    """Require live proxy readiness and a recent route for the benchmark era."""
    if bench_version < 7:
        return True
    if (
        requirements is None
        or not requirements.enabled
        or not requirements.provider_key_configured
    ):
        return False
    cutoff = now - requirements.route_observation_max_age
    if bench_version >= 8:
        route_statement = (
            select(InferenceProviderRoute)
            .join(
                InferenceRoutingPolicy,
                InferenceRoutingPolicy.model == InferenceProviderRoute.model,
            )
            .where(
                InferenceProviderRoute.model == requirements.model,
                InferenceProviderRoute.status == "healthy",
                InferenceProviderRoute.last_observed_at.is_not(None),
                InferenceProviderRoute.last_observed_at >= cutoff,
            )
        )
        if requirements.routing_mode == "aggregate_throughput":
            if (
                requirements.aggregate_provider is None
                or requirements.aggregate_profile_revision is None
            ):
                return False
            route_statement = route_statement.where(
                InferenceProviderRoute.provider == requirements.aggregate_provider,
                InferenceProviderRoute.profile_revision
                == requirements.aggregate_profile_revision,
            )
        elif requirements.routing_mode == "adaptive":
            route_statement = route_statement.where(
                InferenceRoutingPolicy.enabled.is_(True),
                InferenceProviderRoute.provider.is_not(None),
            )
        else:
            return False
        routes = list(await session.scalars(route_statement))
        routes = [
            route
            for route in routes
            if route.cooldown_until is None or route.cooldown_until <= now
        ]
        return bool(routes) and any(
            heartbeat_matches_inference_contract(
                heartbeat,
                now=now,
                bench_version=bench_version,
                model=requirements.model,
                route_contracts=set(),
            )
            for heartbeat in list(await session.scalars(select(ValidatorHeartbeat)))
        )
    if requirements.reviewed_manifest_sha256 is None:
        return False
    route_statement = (
        select(InferenceProviderRoute)
        .join(
            InferenceRoutingPolicy,
            InferenceRoutingPolicy.model == InferenceProviderRoute.model,
        )
        .where(
            InferenceProviderRoute.model == requirements.model,
            InferenceProviderRoute.status == "healthy",
            InferenceProviderRoute.calibration_status == "eligible",
            InferenceProviderRoute.calibration_manifest_sha256
            == requirements.reviewed_manifest_sha256,
            InferenceProviderRoute.last_observed_at.is_not(None),
            InferenceProviderRoute.last_observed_at >= cutoff,
            InferenceProviderRoute.calibration_tool_accuracy
            >= InferenceRoutingPolicy.min_tool_accuracy,
            InferenceProviderRoute.calibration_composite
            >= InferenceRoutingPolicy.min_composite,
        )
    )
    if requirements.routing_mode == "aggregate_throughput":
        if (
            requirements.aggregate_provider is None
            or requirements.aggregate_profile_revision is None
            or requirements.aggregate_calibration_samples is None
        ):
            return False
        route_statement = route_statement.where(
            InferenceProviderRoute.provider == requirements.aggregate_provider,
            InferenceProviderRoute.profile_revision
            == requirements.aggregate_profile_revision,
            InferenceProviderRoute.calibration_sample_count
            == requirements.aggregate_calibration_samples,
        )
    elif requirements.routing_mode == "adaptive":
        route_statement = route_statement.where(
            InferenceRoutingPolicy.enabled.is_(True),
            InferenceProviderRoute.calibration_sample_count
            >= InferenceRoutingPolicy.min_calibration_samples,
            InferenceProviderRoute.ewma_error_rate
            <= InferenceRoutingPolicy.max_error_rate,
            InferenceProviderRoute.ewma_timeout_rate
            <= InferenceRoutingPolicy.max_timeout_rate,
        )
    else:
        return False
    routes = list(await session.scalars(route_statement))
    routes = [
        route
        for route in routes
        if route.cooldown_until is None or route.cooldown_until <= now
    ]
    if not routes:
        return False
    route_contracts = {
        (
            str(route.calibration_manifest_sha256),
            route.provider,
            route.profile_revision,
        )
        for route in routes
    }
    for heartbeat in list(await session.scalars(select(ValidatorHeartbeat))):
        if heartbeat_matches_inference_contract(
            heartbeat,
            now=now,
            bench_version=bench_version,
            model=requirements.model,
            route_contracts=route_contracts,
        ):
            return True
    return False


async def rolling_top_five(session: AsyncSession) -> list[RolloutSnapshotMember]:
    """Return the hybrid top five for the durable rollout transition.

    While a rollout is open, an agent remains ranked by the rollout's source
    median until it has a complete target-version quorum. At quorum its target
    median atomically replaces the source median for this ranking. With no open
    rollout, only the active version is authoritative. No compiled "canary"
    constant is allowed to select or open a benchmark transition.
    """
    rollout = await open_rollout(session)
    source_version = rollout.from_version if rollout is not None else None
    if source_version is None:
        source_version = await active_bench_version(session)
    target_version = rollout.desired_version if rollout is not None else None
    # Column-scoped on purpose. Hydrating ORM ``Score`` entities pulls the
    # per-case ``details`` breakdown -- kilobytes per row, for every scored
    # agent -- and this ranking reads five scalars. Fetching whole rows made an
    # operator's read of the rollout status the most expensive query in the API.
    rows = (
        await session.execute(
            select(
                Agent.agent_id,
                Agent.miner_hotkey,
                Agent.created_at,
                Score.bench_version,
                Score.composite,
                Score.validator_hotkey,
                Score.n,
            )
            .join(Score, Score.agent_id == Agent.agent_id)
            .where(
                Agent.status == AgentStatus.SCORED,
                Score.bench_version.in_(
                    (source_version, target_version)
                    if target_version is not None
                    else (source_version,)
                ),
            )
        )
    ).all()
    if not rows:
        return []
    identities: dict[UUID, tuple[str, datetime]] = {}
    by_agent: dict[UUID, dict[int, list[tuple[float, str, int]]]] = {}
    for row in rows:
        identities[row.agent_id] = (row.miner_hotkey, row.created_at)
        by_agent.setdefault(row.agent_id, {}).setdefault(row.bench_version, []).append(
            (row.composite, row.validator_hotkey, row.n)
        )

    candidates: list[tuple[UUID, float]] = []
    for agent_id, versions in by_agent.items():
        selected = (
            versions.get(target_version, []) if target_version is not None else []
        )
        if target_version is None or len(selected) < SCORING_QUORUM:
            selected = versions.get(source_version, [])
        if not selected:
            continue
        composite, _hotkey, cases = sorted(selected, key=lambda row: (row[0], row[1]))[
            (len(selected) - 1) // 2
        ]
        if cases < 100 or composite <= 0:
            continue
        candidates.append((agent_id, float(composite)))

    from ditto.db.queries.payments import get_miner_coldkeys_for_agents

    coldkeys = await get_miner_coldkeys_for_agents(
        session, agent_ids={agent_id for agent_id, _ in candidates}
    )
    # Keep one best generation per payment-time coldkey before taking the
    # network-wide top five. Legacy rows without payment provenance remain
    # isolated by hotkey.
    candidates.sort(key=lambda item: (-item[1], identities[item[0]][1], item[0]))
    unique: list[RolloutSnapshotMember] = []
    seen_owners: set[str] = set()
    for agent_id, composite in candidates:
        miner_hotkey = identities[agent_id][0]
        owner = (
            f"coldkey:{coldkeys[agent_id]}"
            if agent_id in coldkeys
            else f"hotkey:{miner_hotkey}"
        )
        if owner in seen_owners:
            continue
        seen_owners.add(owner)
        unique.append(RolloutSnapshotMember(agent_id, miner_hotkey, composite))
        if len(unique) == PRIORITY_COHORT_SIZE:
            break
    return unique


async def historical_rescore_cohort(
    session: AsyncSession,
    *,
    source_version: int,
    limit: int = DEFAULT_RESCORE_COHORT_SIZE,
) -> list[RolloutSnapshotMember]:
    """Freeze the prior-era rescore cohort without admitting the whole ledger.

    The immediately previous benchmark owns the cohort. If it has fewer than
    ``limit`` finalized distinct miner families, the next older scored benchmark
    fills the remaining positions. Family identity is the same payment-coldkey
    plus active mutual-attestation graph used by the public ledger; the frozen
    member rows preserve that point-in-time decision if an attestation later
    changes. No third historical era is consulted: this is the explicit
    "combine two previous benchmark iterations" fallback, not an unbounded
    backfill of every legacy submission. That contract is enforced by the
    ``.limit(2)`` on the version query below and is independent of ``limit`` --
    widening the cohort to twenty-five can only return fewer members from the
    same two eras, never reach into a third.

    The upper bound is the storage ceiling, not the default cohort size: an
    operator may configure any size in ``[5, 25]``, so validating against the
    default would make raising the setting throw.
    """
    if not PRIORITY_COHORT_SIZE <= limit <= MAX_PERSISTED_RESCORE_COHORT_SIZE:
        raise ValueError(
            f"rollout cohort limit must be between {PRIORITY_COHORT_SIZE} "
            f"and {MAX_PERSISTED_RESCORE_COHORT_SIZE}"
        )
    versions = list(
        await session.scalars(
            select(Score.bench_version)
            .where(Score.bench_version <= source_version)
            .distinct()
            .order_by(Score.bench_version.desc())
            .limit(2)
        )
    )
    if not versions:
        return []
    agents = list(
        await session.scalars(
            select(Agent).where(
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE))
            )
        )
    )
    if not agents:
        return []
    scores = list(
        await session.scalars(
            select(Score).where(
                Score.agent_id.in_([agent.agent_id for agent in agents]),
                Score.bench_version.in_(versions),
            )
        )
    )
    by_version_agent: dict[int, dict[UUID, list[Score]]] = {}
    for score in scores:
        by_version_agent.setdefault(score.bench_version, {}).setdefault(
            score.agent_id, []
        ).append(score)

    from ditto.db.queries.payments import get_miner_coldkeys_for_agents
    from ditto.db.queries.scores import (
        attested_emission_owner_roots,
        emission_owner,
    )

    coldkeys = await get_miner_coldkeys_for_agents(
        session, agent_ids={agent.agent_id for agent in agents}
    )
    owner_roots = dict(
        zip(
            (agent.agent_id for agent in agents),
            await attested_emission_owner_roots(
                session,
                [
                    (
                        agent.miner_hotkey,
                        emission_owner(
                            miner_hotkey=agent.miner_hotkey,
                            miner_coldkey=coldkeys.get(agent.agent_id),
                        ),
                    )
                    for agent in agents
                ],
            ),
            strict=True,
        )
    )
    agent_by_id = {agent.agent_id: agent for agent in agents}
    selected: list[RolloutSnapshotMember] = []
    seen_agents: set[UUID] = set()
    seen_owners: set[str] = set()
    for version in versions:
        ranked: list[tuple[Agent, float]] = []
        for agent_id, version_scores in by_version_agent.get(version, {}).items():
            # A partial score set is provisional, not a finalized historical
            # standing, and must not consume one of the bounded rescore slots.
            if len(version_scores) < SCORING_QUORUM:
                continue
            middle = sorted(
                version_scores,
                key=lambda row: (row.composite, row.validator_hotkey),
            )[(len(version_scores) - 1) // 2]
            if middle.n < 100 or middle.composite <= 0:
                continue
            ranked.append((agent_by_id[agent_id], float(middle.composite)))
        ranked.sort(key=lambda item: (-item[1], item[0].created_at, item[0].agent_id))
        for agent, composite in ranked:
            owner = owner_roots[agent.agent_id]
            if agent.agent_id in seen_agents or owner in seen_owners:
                continue
            seen_agents.add(agent.agent_id)
            seen_owners.add(owner)
            selected.append(
                RolloutSnapshotMember(agent.agent_id, agent.miner_hotkey, composite)
            )
            if len(selected) == limit:
                return selected
    return selected


async def append_rollout_member(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    member: RolloutSnapshotMember,
    dataset: DatasetPin,
    now: datetime,
    audit_context: dict[str, Any] | None = None,
) -> bool:
    """Permanently qualify one member of the frozen historical cohort."""
    locked = await session.get(
        BenchmarkRollout, rollout.rollout_id, with_for_update=True
    )
    assert locked is not None
    existing = await session.get(
        BenchmarkRolloutMember, (rollout.rollout_id, member.agent_id)
    )
    if existing is not None or locked.status not in (
        "collecting",
        "blocked_ineligible",
    ):
        return False
    if locked.status == "blocked_ineligible":
        locked.status = "collecting"
        locked.blocked_reason = None
    position = (
        int(
            await session.scalar(
                select(
                    func.coalesce(func.max(BenchmarkRolloutMember.position), 0)
                ).where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            )
        )
        + 1
    )
    session.add(
        BenchmarkRolloutMember(
            rollout_id=rollout.rollout_id,
            agent_id=member.agent_id,
            position=position,
            frozen_miner_hotkey=member.miner_hotkey,
            frozen_composite=member.composite,
        )
    )
    existing_dataset = await session.get(
        BenchmarkDataset, (member.agent_id, locked.desired_version)
    )
    if existing_dataset is None:
        session.add(
            BenchmarkDataset(
                agent_id=member.agent_id,
                bench_version=locked.desired_version,
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
        raise ValueError("existing benchmark dataset does not match qualification")
    audit_payload: dict[str, Any] = {
        "agent_id": str(member.agent_id),
        "position": position,
        "hybrid_composite": member.composite,
        "dataset_seed": dataset.seed,
        "dataset_sha256": dataset.sha256,
        "origin": "automatic",
    }
    if audit_context is not None:
        audit_payload.update(audit_context)
    await _audit(
        session,
        locked,
        "member_qualified",
        audit_payload,
        now=now,
    )
    await session.flush()
    return True


async def active_bench_version(session: AsyncSession) -> int:
    open_transition = await open_rollout(session)
    if open_transition is not None:
        from ditto.db.queries.scores import (
            count_ranked_quorum_agents,
            ranked_quorum_agent_ids,
        )

        # The gate width is the target this rollout FROZE at start, not the live
        # operator setting: re-gating a transition already in flight would move
        # its finish line underneath the validators scoring it.
        priority_target = open_transition.priority_cohort_target
        priority_ids = set(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id).where(
                    BenchmarkRolloutMember.rollout_id == open_transition.rollout_id,
                    BenchmarkRolloutMember.position <= priority_target,
                )
            )
        )
        member_ids = set(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id).where(
                    BenchmarkRolloutMember.rollout_id == open_transition.rollout_id
                )
            )
        )
        ranked_priority_ids = await ranked_quorum_agent_ids(
            session,
            bench_version=open_transition.desired_version,
            agent_ids=priority_ids,
        )
        ready = await count_ranked_quorum_agents(
            session,
            bench_version=open_transition.desired_version,
            agent_ids=member_ids,
        )
        # MIN_DESIRED_AUTHORITY_AGENTS stays a constant on purpose. It is the
        # KOTH emission-set size, so it is a consensus quantity, not queue
        # policy: below it the ledger flip would have fewer recipients than the
        # emission split expects. The priority target above is the tunable half.
        if (
            len(priority_ids) == priority_target
            and ranked_priority_ids == priority_ids
            and ready >= MIN_DESIRED_AUTHORITY_AGENTS
        ):
            if (
                open_transition.desired_version >= 7
                and not await inference_activation_ready(
                    session,
                    bench_version=open_transition.desired_version,
                    now=datetime.now(UTC),
                    requirements=session.info.get(
                        _INFERENCE_ACTIVATION_REQUIREMENTS_SESSION_KEY
                    ),
                )
            ):
                return await persisted_active_bench_version(session)
            return open_transition.desired_version
    return await persisted_active_bench_version(session)


async def persisted_active_bench_version(session: AsyncSession) -> int:
    """Return the latest durable benchmark-authority decision.

    Normal rollout activation records authority on the rollout row. Recovery from
    an already-superseded, fully qualified rollout records an append-only
    ``authority_selected`` audit event instead of rewriting terminal history.
    Comparing both timestamps keeps the newest durable authority decision
    authoritative without adding a second mutable state table.
    """
    activated = (
        await session.execute(
            select(BenchmarkRollout.desired_version, BenchmarkRollout.activated_at)
            .where(
                BenchmarkRollout.status == "activated",
                BenchmarkRollout.activated_at.is_not(None),
            )
            .order_by(BenchmarkRollout.activated_at.desc())
            .limit(1)
        )
    ).first()
    selected = (
        await session.execute(
            select(BenchmarkRollout.desired_version, BenchmarkRolloutAudit.recorded_at)
            .join(
                BenchmarkRolloutAudit,
                BenchmarkRolloutAudit.rollout_id == BenchmarkRollout.rollout_id,
            )
            .where(BenchmarkRolloutAudit.event == "authority_selected")
            .order_by(
                BenchmarkRolloutAudit.recorded_at.desc(),
                BenchmarkRolloutAudit.audit_id.desc(),
            )
            .limit(1)
        )
    ).first()
    if selected is not None and (
        activated is None or selected.recorded_at >= activated.activated_at
    ):
        return int(selected.desired_version)
    if activated is not None:
        return int(activated.desired_version)
    # Nothing durable on record: a fresh deployment, or a ledger restored
    # without its rollout history. The honest answer is the FLOOR, not
    # ``DEFAULT_BENCH_VERSION``.
    #
    # That constant is a statement about the beginning of this subnet's history
    # and it is frozen at 2 for the same reason ``LEGACY_BENCH_VERSION`` is. It
    # stopped being a usable answer to "what era are we scoring" the moment v2
    # was retired: ``request_job`` would take it at face value, cut a v2 lease,
    # and the ``validator_tickets_bench_version_floor`` trigger would refuse the
    # insert -- an unhandled IntegrityError surfacing as a 500 on the job-claim
    # path rather than a 204.
    return MIN_SCOREABLE_BENCH_VERSION


async def open_rollout(
    session: AsyncSession, *, for_update: bool = False
) -> BenchmarkRollout | None:
    statement = (
        select(BenchmarkRollout)
        .where(BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")))
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def arrival_bench_version(session: AsyncSession, *, agent: Agent) -> int:
    """Return the benchmark era one submission's *arrival* places it in.

    A submission received after an open rollout starts enters the desired era
    immediately: its dataset is rendered there and validators lease it there
    through the fresh-submission lane. So does an older submission the rollout
    has ADOPTED as previous-generation carryover: it already holds a
    desired-version dataset pin and is leased in the new era, so a later policy
    rescreen must regenerate it there rather than back in the era it was
    stranded in. Other older submissions stay on the active version unless they
    separately qualify for the rollout cohort — cohort membership is a different
    lane with different rules, so it is deliberately not considered here;
    callers that care must check it themselves.

    This mirrors :func:`ditto.db.queries.benchmark_admission.
    benchmark_admission_predicate`'s ``created_at >= rollout.created_at`` and
    carryover disjuncts, in Python, for one already-loaded agent.
    """
    bench_version = await active_bench_version(session)
    rollout = await open_rollout(session)
    if rollout is None:
        return bench_version
    if agent.created_at >= rollout.created_at:
        return int(rollout.desired_version)
    # The EXISTS is inlined on the model rather than delegated to
    # ``ditto.db.queries.benchmark_carryover``: that module needs this one's
    # ``DatasetPin``, so importing it back would be a cycle.
    if await session.scalar(
        select(
            exists().where(
                BenchmarkRolloutCarryover.rollout_id == rollout.rollout_id,
                BenchmarkRolloutCarryover.agent_id == agent.agent_id,
            )
        )
    ):
        return int(rollout.desired_version)
    return bench_version


async def agent_is_rollout_member(
    session: AsyncSession, *, rollout: BenchmarkRollout, agent_id: UUID
) -> bool:
    """Whether one agent is a frozen member of ``rollout``'s inherited cohort."""
    return bool(
        await session.scalar(
            select(
                exists().where(
                    BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                    BenchmarkRolloutMember.agent_id == agent_id,
                )
            )
        )
    )


async def rollout_for_transition(
    session: AsyncSession,
    *,
    from_version: int,
    desired_version: int = CANARY_BENCH_VERSION,
    for_update: bool = False,
) -> BenchmarkRollout | None:
    """Return the durable row for one transition, including after activation."""
    statement = (
        select(BenchmarkRollout)
        .where(
            BenchmarkRollout.from_version == from_version,
            BenchmarkRollout.desired_version == desired_version,
        )
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def rollout_for_desired_version(
    session: AsyncSession, *, desired_version: int
) -> BenchmarkRollout | None:
    """Return the durable row targeting ``desired_version``, whatever it came from."""
    return await session.scalar(
        select(BenchmarkRollout)
        .where(BenchmarkRollout.desired_version == desired_version)
        .order_by(BenchmarkRollout.created_at.desc())
        .limit(1)
    )


async def append_rollout_audit(
    session: AsyncSession,
    rollout: BenchmarkRollout,
    event: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> None:
    """Append one row to a rollout's operator/public-safe history."""
    session.add(
        BenchmarkRolloutAudit(
            audit_id=uuid4(),
            rollout_id=rollout.rollout_id,
            event=event,
            payload=payload,
            recorded_at=now,
        )
    )


# Module-private alias so this module's many existing call sites read unchanged.
# The public name exists because previous-generation carryover lives in its own
# module and must not reach across for a private helper.
_audit = append_rollout_audit


async def supersede_open_rollout(
    session: AsyncSession,
    *,
    actor: str,
    reason: str,
    now: datetime,
) -> BenchmarkRollout | None:
    """Terminally abandon the open rollout so the next one can be opened.

    Returns ``None`` when no rollout is open. Refuses to touch an ``activated``
    rollout: activation already moved chain weights and published the retired
    corpus, so rewriting it would be rewriting history. The partial open index
    excludes ``superseded``, so the single open slot is freed immediately.
    """
    # Deliberately the LATEST row rather than open_rollout(): an activated
    # rollout must produce an explicit refusal, not a misleading "nothing open".
    rollout = await session.scalar(
        select(BenchmarkRollout)
        .order_by(BenchmarkRollout.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if rollout is None or rollout.status == "superseded":
        return None
    if rollout.status == "activated":
        raise RolloutConflictError(
            "an activated benchmark rollout cannot be superseded"
        )
    if await active_bench_version(session) == rollout.desired_version:
        raise RolloutConflictError(
            "a benchmark rollout that already owns active authority cannot be "
            "superseded; select another qualified active contract first"
        )
    previous_status = rollout.status
    rollout.status = "superseded"
    rollout.blocked_reason = None
    await _audit(
        session,
        rollout,
        "superseded",
        {
            "actor": actor,
            "reason": reason,
            "previous_status": previous_status,
            "from_version": rollout.from_version,
            "desired_version": rollout.desired_version,
        },
        now=now,
    )
    await session.flush()
    return rollout


async def authority_selection_state(
    session: AsyncSession, *, bench_version: int
) -> dict[str, Any]:
    """Describe whether a historical contract can safely own weight authority."""
    rollout = await rollout_for_desired_version(session, desired_version=bench_version)
    if rollout is None:
        return {
            "version": bench_version,
            "ready": False,
            "ranked_quorum_agents": 0,
            "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
            "blocked_reason": "no rollout history exists for this contract",
        }
    # This rollout's own frozen gate width, so a historical contract's
    # authority verdict never changes when the live policy is retuned.
    priority_target = rollout.priority_cohort_target
    priority_ids = set(
        await session.scalars(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.position <= priority_target,
            )
        )
    )
    if len(priority_ids) != priority_target:
        return {
            "version": bench_version,
            "ready": False,
            "ranked_quorum_agents": 0,
            "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
            "blocked_reason": "the rollout does not contain a complete priority cohort",
        }
    eligible_ids = set(
        await session.scalars(
            select(Agent.agent_id).where(
                Agent.agent_id.in_(priority_ids),
                Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
            )
        )
    )
    if eligible_ids != priority_ids:
        return {
            "version": bench_version,
            "ready": False,
            "ranked_quorum_agents": 0,
            "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
            "blocked_reason": "one or more priority agents are no longer eligible",
        }
    from ditto.db.queries.scores import count_ranked_quorum_agents

    ranked = await count_ranked_quorum_agents(
        session, bench_version=bench_version, agent_ids=priority_ids
    )
    ready = ranked >= MIN_DESIRED_AUTHORITY_AGENTS
    return {
        "version": bench_version,
        "ready": ready,
        "ranked_quorum_agents": ranked,
        "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
        "blocked_reason": None
        if ready
        else "the priority cohort does not yet have five ranked quorums",
    }


async def select_active_bench_version(
    session: AsyncSession,
    *,
    bench_version: int,
    actor: str,
    reason: str,
    now: datetime,
    inference_requirements: InferenceActivationRequirements | None = None,
) -> BenchmarkRollout:
    """Select a fully qualified historical contract as active authority.

    This is a recovery/control-plane action, not an arbitrary version setter.
    It is forward-only, requires the rollout target to be terminal, and refuses
    to race an open rollout. The append-only audit event becomes the durable
    authority decision while the superseded rollout row remains immutable.
    """
    rows = list(
        (
            await session.execute(
                select(BenchmarkRollout)
                .order_by(BenchmarkRollout.created_at)
                .with_for_update()
            )
        ).scalars()
    )
    if any(row.status in ("collecting", "blocked_ineligible") for row in rows):
        raise RolloutConflictError(
            "supersede the open benchmark rollout before changing active authority"
        )
    current = await persisted_active_bench_version(session)
    if bench_version <= current:
        raise RolloutConflictError(
            f"active benchmark selection is forward-only: current v{current}, "
            f"requested v{bench_version}"
        )
    rollout = next(
        (row for row in reversed(rows) if row.desired_version == bench_version),
        None,
    )
    if rollout is None or rollout.status != "superseded":
        raise RolloutConflictError(
            "only a fully qualified superseded rollout can be selected for recovery"
        )
    readiness = await authority_selection_state(session, bench_version=bench_version)
    if not readiness["ready"]:
        raise RolloutConflictError(str(readiness["blocked_reason"]))
    if not await inference_activation_ready(
        session,
        bench_version=bench_version,
        now=now,
        requirements=inference_requirements,
    ):
        raise RolloutConflictError(
            "benchmark inference is not live on the exact reviewed route"
        )
    await _audit(
        session,
        rollout,
        "authority_selected",
        {
            "actor": actor,
            "reason": reason,
            "previous_active_version": current,
            "bench_version": bench_version,
            "ranked_quorum_agents": readiness["ranked_quorum_agents"],
        },
        now=now,
    )
    await session.flush()
    return rollout


async def create_rollout_snapshot(
    session: AsyncSession,
    *,
    members: Sequence[RolloutSnapshotMember],
    datasets: dict[UUID, DatasetPin],
    now: datetime,
    # No default. A rollout's source era is never "whatever the subnet started
    # on" -- it is the era the fleet is on right now, which the sole caller
    # already passes. Defaulting it to 2 could only ever produce a nonsensical
    # 2 -> target transition, and the floor gives that no useful meaning.
    from_version: int,
    desired_version: int = CANARY_BENCH_VERSION,
    rescore_cohort_target: int = DEFAULT_RESCORE_COHORT_SIZE,
    priority_cohort_target: int = PRIORITY_COHORT_SIZE,
    audit_context: dict[str, Any] | None = None,
) -> BenchmarkRollout:
    """Freeze a bounded prior-era cohort and target dataset pins, idempotently.

    ``rescore_cohort_target`` and ``priority_cohort_target`` are the operator
    policy at the moment of the start. Both are written onto the rollout row and
    never rewritten, so a later policy revision cannot resize or re-gate this
    rollout and a historical rollout can always be explained by the shape it was
    actually built to.
    """
    if desired_version <= from_version:
        raise ValueError("a benchmark rollout must move the version forward")
    if (
        not PRIORITY_COHORT_SIZE
        <= rescore_cohort_target
        <= MAX_PERSISTED_RESCORE_COHORT_SIZE
    ):
        raise ValueError(
            f"rollout rescore cohort target must be between {PRIORITY_COHORT_SIZE} "
            f"and {MAX_PERSISTED_RESCORE_COHORT_SIZE}"
        )
    if (
        not PRIORITY_COHORT_SIZE
        <= priority_cohort_target
        <= MAX_PERSISTED_RESCORE_COHORT_SIZE
    ):
        raise ValueError(
            f"rollout priority cohort target must be between {PRIORITY_COHORT_SIZE} "
            f"and {MAX_PERSISTED_RESCORE_COHORT_SIZE}"
        )
    if priority_cohort_target > rescore_cohort_target:
        # The gate would wait on inherited positions the cohort never fills, so
        # the rollout could never activate. The wire model rejects this too; this
        # is the guard for internal callers that bypass it.
        raise ValueError(
            f"rollout priority cohort target {priority_cohort_target} cannot exceed "
            f"its rescore cohort target {rescore_cohort_target}"
        )
    if session.get_bind().dialect.name == "postgresql":
        # One global rollout lock name, deliberately NOT keyed on the version:
        # only one rollout may be open at a time, so every transition must
        # serialise against every other. The legacy literal is kept so a
        # mid-deploy mix of old and new code still shares the same lock.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended("benchmark-v3-rollout", 0)
                )
            )
        )
    existing = await rollout_for_transition(
        session,
        from_version=from_version,
        desired_version=desired_version,
        for_update=True,
    )
    if existing is not None:
        return existing
    conflicting = await open_rollout(session, for_update=True)
    if conflicting is not None:
        raise RolloutConflictError(
            f"benchmark rollout {conflicting.from_version}->"
            f"{conflicting.desired_version} is still {conflicting.status}; only one "
            "benchmark rollout may be open at a time"
        )
    if not PRIORITY_COHORT_SIZE <= len(members) <= rescore_cohort_target:
        raise ValueError(
            f"a benchmark rollout requires between {PRIORITY_COHORT_SIZE} and "
            f"{rescore_cohort_target} members"
        )
    if len({m.agent_id for m in members}) != len(members):
        raise ValueError("benchmark rollout agents must be distinct")
    if len({m.miner_hotkey for m in members}) != len(members):
        raise ValueError("benchmark rollout miners must be distinct")
    if set(datasets) != {m.agent_id for m in members}:
        raise ValueError("every frozen rollout member requires one target dataset pin")

    rollout = BenchmarkRollout(
        rollout_id=uuid4(),
        from_version=from_version,
        desired_version=desired_version,
        status="collecting",
        cohort_size=len(members),
        rescore_cohort_target=rescore_cohort_target,
        priority_cohort_target=priority_cohort_target,
        created_at=now,
    )
    session.add(rollout)
    for position, member in enumerate(members, start=1):
        session.add(
            BenchmarkRolloutMember(
                rollout_id=rollout.rollout_id,
                agent_id=member.agent_id,
                position=position,
                frozen_miner_hotkey=member.miner_hotkey,
                frozen_composite=member.composite,
            )
        )
        pin = datasets[member.agent_id]
        existing_dataset = await session.get(
            BenchmarkDataset, (member.agent_id, desired_version)
        )
        if existing_dataset is None:
            session.add(
                BenchmarkDataset(
                    agent_id=member.agent_id,
                    bench_version=desired_version,
                    seed=pin.seed,
                    sha256=pin.sha256,
                    run_size=pin.run_size,
                    seed_block=pin.seed_block,
                    seed_block_hash=pin.seed_block_hash,
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
            pin.seed,
            pin.sha256,
            pin.run_size,
            pin.seed_block,
            pin.seed_block_hash,
        ):
            raise ValueError("existing benchmark dataset does not match snapshot")
    audit_payload: dict[str, Any] = {
        "agent_ids": [str(member.agent_id) for member in members]
    }
    if audit_context:
        audit_payload.update(audit_context)
    await _audit(session, rollout, "cohort_frozen", audit_payload, now=now)
    await session.flush()
    return rollout


async def _validate_frozen_members(
    session: AsyncSession, rollout: BenchmarkRollout, *, now: datetime
) -> bool:
    rows = (
        await session.execute(
            select(BenchmarkRolloutMember, Agent)
            .join(Agent, Agent.agent_id == BenchmarkRolloutMember.agent_id)
            .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            .order_by(BenchmarkRolloutMember.position)
        )
    ).all()
    permanently_ineligible = {
        AgentStatus.SCREENING_FAILED,
        AgentStatus.QUARANTINED,
        AgentStatus.REJECTED,
        AgentStatus.BANNED,
    }
    invalid = [
        str(member.agent_id)
        for member, agent in rows
        if agent.status in permanently_ineligible
    ]
    if len(rows) != rollout.cohort_size or invalid:
        reason = (
            "frozen cohort is incomplete"
            if len(rows) != rollout.cohort_size
            else "ineligible frozen members: " + ",".join(invalid)
        )
        if rollout.status != "blocked_ineligible" or rollout.blocked_reason != reason:
            rollout.status = "blocked_ineligible"
            rollout.blocked_reason = reason
            await _audit(
                session, rollout, "cohort_blocked", {"reason": reason}, now=now
            )
        return False
    if rollout.status == "blocked_ineligible":
        rollout.status = "collecting"
        rollout.blocked_reason = None
        await _audit(session, rollout, "cohort_unblocked", {}, now=now)
    return True


def protocol_serves_version(protocol_version: int, *, version: int) -> bool:
    """Whether a heartbeat of this protocol can advertise ``version`` at all.

    Protocol 8 is the floor at which a heartbeat carries a SIGNED capability and
    stack-identity payload at all -- it is a wire-format floor, not a
    per-benchmark one, so it stays fixed as the benchmark version moves. Any
    validator that can advertise a post-v2 benchmark is already >= 8.

    Split out because failing *this* clause is categorically different from
    failing the ones below it: the validator's software cannot describe the
    benchmark being scored, so no probe result and no restart will change the
    answer until it is upgraded.
    """
    if protocol_version < 8:
        return False
    return not (version >= 7 and protocol_version < 12)


def verified_scorer_for_version(
    heartbeat: ValidatorHeartbeat,
    *,
    version: int,
) -> ScorerBenchmarkCapability | None:
    """The signed scorer capability that advertises ``version``, else ``None``.

    Capability only: every clause here is about what this validator's stack *is*,
    never about when it last said so. Liveness and observation freshness belong
    to :func:`heartbeat_supports_version`, which adds them before leasing work.
    Split out so a caller that wants to explain a validator's standing -- the
    public fleet view naming a legacy stack that can never score the active
    benchmark -- does not have to call a quiet validator incapable.
    """
    if not protocol_serves_version(heartbeat.protocol_version, version=version):
        return None
    try:
        capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(heartbeat.capabilities)
        )
        stack = ValidatorStackIdentity.model_validate_json(json.dumps(heartbeat.stack))
    except ValidationError:
        return None
    scorer = capabilities.scorer_benchmarks
    if (
        scorer is None
        or scorer.status != "fresh_verified"
        or version not in scorer.supported_bench_versions
    ):
        return None
    if version >= 9 and not _scorer_version_at_least(
        scorer.software_version, minimum=_V9_MINIMUM_SCORER_VERSION
    ):
        return None
    if version >= 7 and (
        not capabilities.ticket_inference or not capabilities.signed_score_quorum
    ):
        return None
    # The calibration manifest is part of the retired v7 scoring contract, not
    # a general requirement for every later benchmark. A v8-only scorer must not
    # carry v7 metadata merely to pass Platform's capability gate.
    if version == 7 and scorer.v7_calibration is None:
        return None
    # V8 accepts arbitrary-language miner images, so a scorer binary claiming
    # support is insufficient. Require an explicit signed executor boundary as
    # well. A privileged DinD executor is valid when the scorer's own policy
    # does not require rootless execution; ``unknown`` remains fail-closed.
    # V7 remains routable during the additive migration.
    if version >= 8 and capabilities.executor_isolation not in {
        "privileged_dind",
        "rootless_dind",
        "rootless_host",
        "ephemeral_vm",
    }:
        return None
    component = stack.components.dittobench_api
    if not (
        capabilities.screened_images
        and component.source_revision == scorer.source_revision
        and (component.version is None or component.version == scorer.software_version)
    ):
        return None
    return scorer


def _scorer_version_at_least(
    value: str | None, *, minimum: tuple[int, int, int]
) -> bool:
    """Compare a stable scorer release without trusting free-form labels.

    Source builds and prerelease labels intentionally fail closed. They may
    regain v9 eligibility by running a scorer that reports a stable release
    containing the complete hosted-embedding contract.
    """
    if value is None:
        return False
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdecimal() for part in parts):
        return False
    return tuple(int(part) for part in parts) >= minimum


def heartbeat_matches_inference_contract(
    heartbeat: ValidatorHeartbeat,
    *,
    now: datetime,
    bench_version: int,
    model: str,
    route_contracts: set[tuple[str, str, str]],
) -> bool:
    """Bind a capable scorer to the inference evidence its era publishes.

    V7 is the only scorer era that advertised provider-route calibration in its
    signed heartbeat. V8 and v9 deliberately removed that retired payload: the
    Platform proves recent route health and identity, while the heartbeat binds
    the exact scorer binary and supported benchmark version. Requiring v7-only
    calibration metadata from later scorers makes rollout activation impossible
    rather than safer, so keep the two proofs separate for post-v7 contracts.
    """
    if not heartbeat_supports_version(heartbeat, now=now, version=bench_version):
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(heartbeat.capabilities)
        )
    except ValidationError:
        return False
    if bench_version >= 8:
        return True
    scorer = capabilities.scorer_benchmarks
    calibration = scorer.v7_calibration if scorer is not None else None
    return bool(
        calibration is not None
        and capabilities.ticket_inference
        and any(
            route.model == model
            and (
                calibration.manifest_sha256,
                route.provider,
                route.profile_revision,
            )
            in route_contracts
            for route in calibration.supported_routes
        )
    )


def heartbeat_supports_version(
    heartbeat: ValidatorHeartbeat,
    *,
    now: datetime,
    version: int = CANARY_BENCH_VERSION,
) -> bool:
    """Accept ``version`` only from a fresh scorer report matching its identity."""
    seen_at = (
        heartbeat.seen_at.replace(tzinfo=UTC)
        if heartbeat.seen_at.tzinfo is None
        else heartbeat.seen_at
    )
    if now - seen_at > timedelta(minutes=5):
        return False
    scorer = verified_scorer_for_version(heartbeat, version=version)
    if scorer is None:
        return False
    return (
        scorer.observed_at is not None
        and abs(int(now.timestamp()) - scorer.observed_at) <= 300
    )


async def capable_validator_counts(
    session: AsyncSession,
    *,
    versions: Sequence[int],
    now: datetime | None = None,
) -> dict[int, int]:
    """Count fresh, identity-matched scorers for many versions in one read.

    :func:`rollout_state` answers this for a single version, but it derives the
    whole cohort and quorum picture on the way. Target discovery needs the count
    for every shipped contract, and paying the cohort derivation once per
    contract is what made the operator's read of the rollout status scale with
    the number of shipped benchmarks rather than staying flat.
    """
    now = now or datetime.now(UTC)
    heartbeats = list(await session.scalars(select(ValidatorHeartbeat)))
    return {
        version: sum(
            heartbeat_supports_version(heartbeat, now=now, version=version)
            for heartbeat in heartbeats
        )
        for version in versions
    }


async def rollout_cohort_complete(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    cohort_size: int,
) -> bool:
    """Return whether every ranked member through ``cohort_size`` has quorum."""
    member_ids = set(
        await session.scalars(
            select(BenchmarkRolloutMember.agent_id).where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.position <= cohort_size,
            )
        )
    )
    if len(member_ids) != cohort_size:
        return False
    # Raw row counts are insufficient: smoke-profile and zero-composite scores
    # cannot rank or activate a benchmark. Keep this gate byte-for-byte aligned
    # with rollout activation and the authoritative ledger definition.
    from ditto.db.queries.scores import count_ranked_quorum_agents

    return (
        await count_ranked_quorum_agents(
            session,
            bench_version=rollout.desired_version,
            agent_ids=member_ids,
        )
        == cohort_size
    )


async def rollout_cohort_score_complete(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    cohort_size: int,
) -> bool:
    """Return whether every ranked member has the frozen raw 3/3 barrier."""
    count_rows = (
        await session.execute(
            select(
                BenchmarkRolloutMember.agent_id,
                func.count(Score.validator_hotkey),
            )
            .outerjoin(
                Score,
                (Score.agent_id == BenchmarkRolloutMember.agent_id)
                & (Score.bench_version == rollout.desired_version),
            )
            .where(
                BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
                BenchmarkRolloutMember.position <= cohort_size,
            )
            .group_by(BenchmarkRolloutMember.agent_id)
        )
    ).all()
    return len(count_rows) == cohort_size and all(
        int(count) >= SCORING_QUORUM for _, count in count_rows
    )


async def issue_rollout_ticket(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    now: datetime,
    ttl: timedelta,
    artifact_mode: Literal["legacy", "prefer_screened", "screened_only"] = "legacy",
    validator_running_benchmark: bool = False,
    slot_id: str = "slot-0",
) -> ValidatorTicket | None:
    """Issue one cohort lease, balanced one score per agent per coverage round."""
    # Retained as a keyword-compatible parameter for mixed platform callers;
    # the version contract, not an operator-wide routing flag, governs this.
    _ = artifact_mode
    from ditto.db.queries.tickets import (
        expire_overdue_tickets,
        retry_budget_spent,
        ticket_attempt_cap,
    )

    rollout = await open_rollout(session, for_update=True)
    if rollout is None:
        return None
    # Mirrors the canonical issuance path. This lane could previously be entered
    # without any sweep, so the only thing clearing an overdue lease off the slot
    # was the revocation below -- which is why that query had to match overdue
    # rows and could not carry a ``deadline > now`` filter. Sweeping first lets
    # the revocation narrow to genuinely live leases without stranding the slot
    # behind the one-issued-per-validator-slot unique index.
    await expire_overdue_tickets(session, now=now)
    heartbeat = await session.get(ValidatorHeartbeat, validator_hotkey)
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=rollout.desired_version
    ):
        return None
    contract = benchmark_contract(rollout.desired_version)
    complete_screened_image = (
        Agent.screened_image_sha256.is_not(None)
        & Agent.screened_image_size_bytes.is_not(None)
        & Agent.screened_image_id.is_not(None)
        & Agent.screened_image_ref.is_not(None)
        & Agent.screened_image_upload_id.is_not(None)
        & Agent.screened_image_verified_at.is_not(None)
    )
    existing_statement = (
        select(ValidatorTicket)
        .join(
            BenchmarkRolloutMember,
            BenchmarkRolloutMember.agent_id == ValidatorTicket.agent_id,
        )
        .join(Agent, Agent.agent_id == ValidatorTicket.agent_id)
        .where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.bench_version == rollout.desired_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
            ValidatorTicket.purpose_revision > 0,
            ValidatorTicket.deadline > now,
        )
        .limit(1)
        .with_for_update()
    )
    # ATH review is an emissions hold, not a scoring hold. The score endpoint
    # deliberately accepts reports for ATH_PENDING_REVIEW so evidence gathered
    # during review survives a later clear. Keep leasing the frozen member here
    # as well; authority/activation still excludes it until an operator clears
    # the hold via count_ranked_quorum_agents and maybe_activate_rollout.
    rollout_scoreable_statuses = (
        AgentStatus.SCORED,
        AgentStatus.LIVE,
        AgentStatus.ATH_PENDING_REVIEW,
    )
    existing_statement = existing_statement.where(
        Agent.status.in_(rollout_scoreable_statuses),
        Agent.screening_policy_version >= contract.minimum_screening_policy_version,
        complete_screened_image,
    )
    existing = await session.scalar(existing_statement)
    if existing is not None:
        return existing
    score_count = (
        select(func.count(Score.validator_hotkey))
        .where(
            Score.agent_id == BenchmarkRolloutMember.agent_id,
            Score.bench_version == rollout.desired_version,
        )
        .correlate(BenchmarkRolloutMember)
        .scalar_subquery()
    )
    occupied_count = (
        select(func.count(ValidatorTicket.validator_hotkey))
        .where(
            ValidatorTicket.agent_id == BenchmarkRolloutMember.agent_id,
            ValidatorTicket.bench_version == rollout.desired_version,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .correlate(BenchmarkRolloutMember)
        .scalar_subquery()
    )
    already_scored = (
        select(Score.agent_id)
        .where(
            Score.agent_id == BenchmarkRolloutMember.agent_id,
            Score.bench_version == rollout.desired_version,
            Score.validator_hotkey == validator_hotkey,
        )
        .exists()
    )
    already_ticketed = (
        select(ValidatorTicket.agent_id)
        .where(
            ValidatorTicket.agent_id == BenchmarkRolloutMember.agent_id,
            ValidatorTicket.bench_version == rollout.desired_version,
            ValidatorTicket.validator_hotkey == validator_hotkey,
            (
                ValidatorTicket.status.in_((TicketStatus.ISSUED, TicketStatus.SCORED))
                | (
                    (ValidatorTicket.status == TicketStatus.EXPIRED)
                    & ((ValidatorTicket.retry_after > now) | retry_budget_spent())
                )
            ),
        )
        .exists()
    )
    # This rollout's frozen gate width, so retuning the live policy never
    # changes which members an in-flight rollout prioritises.
    priority_target = rollout.priority_cohort_target
    priority_complete = await rollout_cohort_score_complete(
        session,
        rollout=rollout,
        cohort_size=priority_target,
    )
    member_statement = (
        select(BenchmarkRolloutMember)
        .join(Agent, Agent.agent_id == BenchmarkRolloutMember.agent_id)
        .where(
            BenchmarkRolloutMember.rollout_id == rollout.rollout_id,
            Agent.status.in_(rollout_scoreable_statuses),
            Agent.screening_policy_version >= contract.minimum_screening_policy_version,
            complete_screened_image,
            ~already_scored,
            ~already_ticketed,
            score_count + occupied_count < SCORING_QUORUM,
        )
    )

    def ordered_candidate(statement: Select) -> Select:
        return (
            statement.order_by(
                (score_count + occupied_count).asc(),
                BenchmarkRolloutMember.position,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    if priority_complete:
        member = await session.scalar(ordered_candidate(member_statement))
    else:
        # Preserve the rollout's priority contract without parking capacity.
        # Every validator first asks for work in the frozen activation cohort.
        # If this validator already scored or holds every incomplete priority
        # member, another lease from it cannot move the activation gate. Let
        # that otherwise-idle slot score the tail while the validators that can
        # still close priority quorum continue to receive priority work first.
        #
        # Activation remains fleet-wide and still requires every priority
        # member to reach quorum; this fallback changes issuance only. A locked
        # priority row is already being assigned by another transaction, so
        # SKIP LOCKED may also fall through safely instead of idling a sibling
        # slot behind the same in-flight allocation.
        member = await session.scalar(
            ordered_candidate(
                member_statement.where(
                    BenchmarkRolloutMember.position <= priority_target
                )
            )
        )
        if member is None:
            member = await session.scalar(
                ordered_candidate(
                    member_statement.where(
                        BenchmarkRolloutMember.position > priority_target
                    )
                )
            )
    if member is None:
        return None
    # A rollout can open while this validator still owns an ordinary source-
    # version lease. Preserve genuinely running work, but an idle/polling
    # validator must not keep resuming that lower-priority lease ahead of the
    # target-version cohort. The database allows only one issued ticket per
    # validator slot across all benchmark versions, so release any non-resumable
    # lease only after proving that eligible rollout work exists.
    competing_ticket = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.status == TicketStatus.ISSUED,
            # Overdue leases are the deadline sweep's business, not a
            # revocation's: the sweep above has already flipped them and set the
            # cooldown from the deadline rather than from now. Without this the
            # sweep and this branch disagreed about the same row.
            ValidatorTicket.deadline > now,
        )
        .limit(1)
        .with_for_update()
    )
    if competing_ticket is not None:
        if (
            competing_ticket.purpose != TicketPurpose.CANONICAL_QUORUM
            or competing_ticket.purpose_revision <= 0
        ):
            return None
        # Same fail-safe rule as the canonical issuance path, through the same
        # gate: revoke only on fresh, post-issuance, positive evidence that the
        # slot is idle. This copy was the looser of the two (it matched overdue
        # leases as well), so routing both through one helper is what keeps them
        # from drifting apart again.
        from ditto.db.queries.lease_liveness import maybe_force_expire_lease

        if not await maybe_force_expire_lease(
            session,
            ticket=competing_ticket,
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            now=now,
            context="issue_rollout_ticket",
            running_benchmark_reported=validator_running_benchmark,
            requested_bench_version=rollout.desired_version,
        ):
            return None
    ticket = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id == member.agent_id,
            ValidatorTicket.bench_version == rollout.desired_version,
            ValidatorTicket.validator_hotkey == validator_hotkey,
        )
        .with_for_update()
    )
    if ticket is None:
        ticket = ValidatorTicket(
            agent_id=member.agent_id,
            bench_version=rollout.desired_version,
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            status=TicketStatus.ISSUED,
            purpose=TicketPurpose.CANONICAL_QUORUM,
            purpose_revision=1,
            issued_at=now,
            deadline=now + ttl,
            attempt_count=1,
            manual_retry_grants=0,
        )
        session.add(ticket)
    else:
        retry_after = ticket.retry_after
        if retry_after is not None and retry_after.tzinfo is None:
            retry_after = retry_after.replace(tzinfo=UTC)
        # The candidate predicate above is the fast path, but fail_job can
        # mutate the ticket between that read and this row lock. Re-check the
        # bounded retry contract under the lock so a rollout lease cannot skip
        # its cooldown or mint attempts past its cap during that race.
        if (
            ticket.status != TicketStatus.EXPIRED
            or (retry_after is not None and retry_after > now)
            or ticket.attempt_count >= ticket_attempt_cap(ticket)
        ):
            return None
        ticket.status = TicketStatus.ISSUED
        ticket.purpose = TicketPurpose.CANONICAL_QUORUM
        ticket.purpose_revision += 1
        ticket.legacy_completion_allowed = False
        ticket.slot_id = slot_id
        ticket.issued_at = now
        ticket.deadline = now + ttl
        ticket.attempt_count += 1
        ticket.retry_after = None
        ticket.first_reported_at = None
    await session.flush()
    return ticket


async def maybe_activate_rollout(
    session: AsyncSession,
    rollout: BenchmarkRollout,
    *,
    now: datetime,
    inference_requirements: InferenceActivationRequirements | None = None,
) -> bool:
    """Activate after every frozen cohort member reaches desired quorum."""
    # A superseded (or already activated) rollout is terminal and must never be
    # revived by a refresh sweep that still holds a stale reference to it.
    if rollout.status not in ("collecting", "blocked_ineligible"):
        return False
    if not await _validate_frozen_members(session, rollout, now=now):
        await session.flush()
        return False
    count_rows = (
        await session.execute(
            select(BenchmarkRolloutMember.agent_id, func.count(Score.validator_hotkey))
            .outerjoin(
                Score,
                (Score.agent_id == BenchmarkRolloutMember.agent_id)
                & (Score.bench_version == rollout.desired_version),
            )
            .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            .group_by(BenchmarkRolloutMember.agent_id)
        )
    ).all()
    counts: dict[UUID, int] = {agent_id: int(count) for agent_id, count in count_rows}
    # Fewer than quorum is "not ready yet"; MORE than quorum is still ready. An
    # equality test deadlocks the rollout permanently the first time any member
    # picks up a 4th score (a retry grant, an admin recovery, or a lost race),
    # with no operator escape hatch.
    member_rows = (
        await session.execute(
            select(BenchmarkRolloutMember, Agent)
            .join(Agent, Agent.agent_id == BenchmarkRolloutMember.agent_id)
            .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
        )
    ).all()
    if len(member_rows) != rollout.cohort_size:
        return False
    if any(
        agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE)
        for _member, agent in member_rows
    ):
        return False
    member_ids = {member.agent_id for member, _agent in member_rows}
    if any(counts.get(agent_id, 0) < SCORING_QUORUM for agent_id in member_ids):
        return False
    # Activation is the LAST point at which the full-emission-set guarantee can
    # be enforced. Before activation, list_eligible_ledger's own threshold holds
    # the ledger on the active version until MIN_DESIRED_AUTHORITY_AGENTS agents
    # hold a ranked desired-version quorum. After activation open_rollout()
    # returns None, so desired_version is None, the ledger reads the desired
    # version unconditionally, and that threshold is bypassed entirely — an agent
    # without desired-version scores simply drops out.
    #
    # The raw counts above do not imply rankability: a smoke-profile 3/3 can
    # satisfy them without ever being eligible for weights. Require every
    # frozen cohort member to hold a ranked quorum before closing the rollout.
    from ditto.db.queries.scores import (
        count_ranked_quorum_agents,
        ranked_quorum_agent_ids,
    )

    ranked_member_ids = await ranked_quorum_agent_ids(
        session,
        bench_version=rollout.desired_version,
        agent_ids=member_ids,
    )
    ranked_cohort_agents = await count_ranked_quorum_agents(
        session,
        bench_version=rollout.desired_version,
        agent_ids=member_ids,
    )
    if ranked_member_ids != member_ids:
        return False
    if ranked_cohort_agents < MIN_DESIRED_AUTHORITY_AGENTS:
        return False
    if not await inference_activation_ready(
        session,
        bench_version=rollout.desired_version,
        now=now,
        requirements=inference_requirements,
    ):
        return False
    rollout.status = "activated"
    rollout.activated_at = now
    rollout.blocked_reason = None
    await _audit(
        session,
        rollout,
        "activated",
        {
            "bench_version": rollout.desired_version,
            "score_counts": {str(k): v for k, v in counts.items()},
            "ranked_quorum_agents": ranked_cohort_agents,
        },
        now=now,
    )
    await session.flush()
    return True


async def rollout_state(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    capability_version: int | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    rollout = await session.scalar(
        select(BenchmarkRollout).order_by(BenchmarkRollout.created_at.desc()).limit(1)
    )
    # Single source of truth for the active version: whatever the weight-setting
    # guard (`active_bench_version`) resolves. This endpoint's `active_version` is
    # what operators read and echo back as `expected_active_version` when starting
    # a rollout, so deriving it from the same authority the start guard checks means
    # the two can never disagree and spuriously 409 ("active benchmark changed").
    # In the normal open-rollout case this is identical to the row-derived value
    # (the flip predicates are equivalent when MIN_DESIRED_AUTHORITY_AGENTS ==
    # PRIORITY_COHORT_SIZE); it only reconciles the terminal/edge cases where the
    # most-recent row and the latest activated row differ.
    active_version = await active_bench_version(session)
    version = capability_version or (
        rollout.desired_version if rollout is not None else active_version
    )
    heartbeats = (await session.execute(select(ValidatorHeartbeat))).scalars().all()
    capable_count = sum(
        heartbeat_supports_version(heartbeat, now=now, version=version)
        for heartbeat in heartbeats
    )
    # The authority switch is gated on this count, not on the cohort's raw score
    # counts: the whole ledger flips to the desired version only once at least
    # MIN_DESIRED_AUTHORITY_AGENTS agents hold a complete RANKED quorum there, so
    # the emission set (champion + tail) is never short. Exposing it is the only
    # way a reader can answer "when do weights switch?" without re-deriving it.
    from ditto.db.queries.scores import count_ranked_quorum_agents

    if rollout is None:
        return {
            "active_version": active_version,
            "desired_version": active_version,
            "status": "inactive",
            "capability_bench_version": version,
            # The era the fleet is ACTUALLY on, not DEFAULT_BENCH_VERSION.
            #
            # This is the "when do weights switch?" telemetry, and with no
            # rollout open it was counting quorum agents at v2 while the fleet
            # scored v7 -- answering the question about an era nobody is in.
            # The floor makes that unambiguous rather than merely stale: nothing
            # new can ever be written at v2, so the count could only shrink
            # toward the historical remainder.
            "ranked_quorum_agents": await count_ranked_quorum_agents(
                session, bench_version=active_version
            ),
            "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
            "canary_capable_validator_count": capable_count,
            # DEPRECATED alias of canary_capable_validator_count. It counts
            # validators capable of capability_bench_version, which is no longer
            # always 3. Kept because it is public API; read the new key.
            "v3_capable_validator_count": capable_count,
            "current_hybrid_top_five": [],
            "qualification_converged": False,
            "cohort_size": 0,
            "cohort_ready_count": 0,
            # No rollout row, so nothing has frozen a target yet. The size the
            # NEXT start will freeze is the operator policy, served by
            # GET /admin/queue-policy-settings.
            "rescore_cohort_target": None,
            "max_rescore_cohort_size": MAX_PERSISTED_RESCORE_COHORT_SIZE,
            "priority_cohort_size": PRIORITY_COHORT_SIZE,
            "priority_cohort_target": None,
            "priority_complete": False,
            "members": [],
        }
    count_rows = (
        await session.execute(
            select(BenchmarkRolloutMember.agent_id, func.count(Score.validator_hotkey))
            .outerjoin(
                Score,
                (Score.agent_id == BenchmarkRolloutMember.agent_id)
                & (Score.bench_version == rollout.desired_version),
            )
            .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
            .group_by(BenchmarkRolloutMember.agent_id)
        )
    ).all()
    counts: dict[UUID, int] = {agent_id: int(count) for agent_id, count in count_rows}
    members = (
        (
            await session.execute(
                select(BenchmarkRolloutMember)
                .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
                .order_by(BenchmarkRolloutMember.position)
            )
        )
        .scalars()
        .all()
    )
    current_top = await rolling_top_five(session)
    current_top_ids = {member.agent_id for member in current_top}
    qualified_ids = {member.agent_id for member in members}
    # This rollout's frozen activation gate width.
    priority_target = rollout.priority_cohort_target
    ranked_quorum_agents = await count_ranked_quorum_agents(
        session,
        bench_version=rollout.desired_version,
        agent_ids={member.agent_id for member in members},
    )
    cohort_ready_count = sum(
        counts.get(member.agent_id, 0) >= SCORING_QUORUM for member in members
    )
    priority_members = [
        member for member in members if member.position <= priority_target
    ]
    priority_complete = len(priority_members) == priority_target and all(
        counts.get(member.agent_id, 0) >= SCORING_QUORUM for member in priority_members
    )
    return {
        "active_version": active_version,
        "desired_version": rollout.desired_version,
        "status": rollout.status,
        "blocked_reason": rollout.blocked_reason,
        "capability_bench_version": version,
        "ranked_quorum_agents": ranked_quorum_agents,
        "min_ranked_quorum_agents": MIN_DESIRED_AUTHORITY_AGENTS,
        "canary_capable_validator_count": capable_count,
        # DEPRECATED alias of canary_capable_validator_count; see above.
        "v3_capable_validator_count": capable_count,
        "current_hybrid_top_five": [str(member.agent_id) for member in current_top],
        # Deliberately still PRIORITY_COHORT_SIZE: `current_top` is
        # `rolling_top_five`, the KOTH top five, which is a consensus quantity
        # rather than this rollout's activation gate. Swapping it for the
        # tunable target would silently redefine "converged".
        "qualification_converged": len(current_top) == PRIORITY_COHORT_SIZE
        and current_top_ids.issubset(qualified_ids),
        "cohort_size": rollout.cohort_size,
        "cohort_ready_count": cohort_ready_count,
        # What this rollout froze at start. Immune to later policy revisions,
        # so a historical rollout is always explainable by the size it aimed
        # for -- `cohort_size` is how many members it actually qualified.
        "rescore_cohort_target": rollout.rescore_cohort_target,
        "max_rescore_cohort_size": MAX_PERSISTED_RESCORE_COHORT_SIZE,
        # The gate width this rollout froze at start. Identical to
        # PRIORITY_COHORT_SIZE for every rollout that predates the setting.
        "priority_cohort_size": priority_target,
        "priority_cohort_target": priority_target,
        "priority_complete": priority_complete,
        "members": [
            {
                "agent_id": str(member.agent_id),
                "position": member.position,
                "score_count": int(counts.get(member.agent_id, 0)),
                "currently_top_five": member.agent_id in current_top_ids,
            }
            for member in members
        ],
    }
