"""Application service for convergent rolling benchmark qualification."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.inference_routing import (
    AGGREGATE_CALIBRATION_SAMPLES,
    AGGREGATE_PROVIDER,
    aggregate_profile_revision,
    benchmark_model,
)
from ditto.api_server.queue_policy_settings import resolve_queue_policy_settings
from ditto.db.models import (
    Agent,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    Score,
)
from ditto.db.queries.benchmark_carryover import (
    StrandedCandidate,
    adopt_carryover_agent,
    stranded_prev_gen_candidates,
)
from ditto.db.queries.benchmark_rollout import (
    MAX_PERSISTED_RESCORE_COHORT_SIZE,
    PRIORITY_COHORT_SIZE,
    DatasetPin,
    InferenceActivationRequirements,
    RolloutConflictError,
    RolloutSnapshotMember,
    active_bench_version,
    append_rollout_audit,
    append_rollout_member,
    historical_rescore_cohort,
    maybe_activate_rollout,
    open_rollout,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ditto.api_server.config import InferenceProxyConfig


def inference_activation_requirements(
    config: InferenceProxyConfig | None, *, bench_version: int
) -> InferenceActivationRequirements | None:
    """Snapshot live process configuration without exposing provider secrets."""
    if config is None:
        return None
    model = benchmark_model(bench_version)
    return InferenceActivationRequirements(
        enabled=config.enabled,
        provider_key_configured=bool(config.openrouter_api_key),
        model=model,
        routing_mode=config.routing_mode,  # type: ignore[arg-type]
        reviewed_manifest_sha256=config.reviewed_calibration_manifest_sha256,
        aggregate_provider=AGGREGATE_PROVIDER,
        aggregate_profile_revision=aggregate_profile_revision(model),
        aggregate_calibration_samples=AGGREGATE_CALIBRATION_SAMPLES,
        route_observation_max_age=timedelta(
            seconds=max(60, config.discovery_interval_seconds)
        ),
    )


@dataclass(frozen=True)
class PendingQualification:
    member: RolloutSnapshotMember
    seed: int
    run_size: str
    seed_block: int | None
    seed_block_hash: str | None
    seed_source: Literal[
        "legacy_pin",
        "source_scores_canonical_min",
        "historical_scores_latest_min",
        "versioned_pin",
    ]
    existing_sha256: str | None = None


@dataclass(frozen=True)
class PendingCarryover:
    """A stranded prior-era submission plus the target-version input it needs.

    ``qualification.member`` is a synthetic :class:`RolloutSnapshotMember` built
    only so :func:`qualification_candidate`'s seed resolution can be reused; its
    ``composite`` is meaningless here and is never persisted. Carryover rows have
    no frozen composite because carryover is not a ranked cohort position.
    """

    candidate: StrandedCandidate
    qualification: PendingQualification


@dataclass(frozen=True)
class RolloutExpansionResult:
    previous_target: int
    new_target: int
    appended_members: int


async def _rollout_rescore_cohort(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    target_override: int | None = None,
) -> list[RolloutSnapshotMember]:
    """Preserve already-frozen members, then fill the rollout to ITS OWN target.

    The size read here is ``rollout.rescore_cohort_target`` -- the operator
    policy frozen onto this row when the rollout started -- never the live
    setting. That is the whole answer to "what happens if an operator raises the
    cohort size to 25 while a 10-member rollout is open?": nothing. This rollout
    keeps filling to 10 and the new value applies to the next rollout only.

    The alternative (reading the live policy) would let a backroom write grow an
    in-flight cohort underneath the validators already scoring it, silently
    moving the completion bar that gates the authority switch. Membership being
    append-only makes that *safe*, but it does not make it *predictable*, and an
    operator raising a policy default should not extend an in-flight transition
    they have already been told the shape of.

    Older deployments created cohorts as large as 25. Those durable rows and
    their accepted scores are never deleted; a rollout already at or above its
    target is returned untouched.
    """
    existing = (
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
    if len(existing) > MAX_PERSISTED_RESCORE_COHORT_SIZE:
        raise RuntimeError("existing benchmark rollout exceeds the top-25 bound")
    target = (
        rollout.rescore_cohort_target if target_override is None else target_override
    )
    if not PRIORITY_COHORT_SIZE <= target <= MAX_PERSISTED_RESCORE_COHORT_SIZE:
        # A CHECK constraint already guarantees this; fail loudly rather than
        # silently rescoring an unbounded cohort if a row is ever hand-edited.
        raise RuntimeError(
            f"benchmark rollout rescore target {target} is outside "
            f"[{PRIORITY_COHORT_SIZE}, {MAX_PERSISTED_RESCORE_COHORT_SIZE}]"
        )
    cohort = [
        RolloutSnapshotMember(
            member.agent_id,
            member.frozen_miner_hotkey,
            member.frozen_composite,
        )
        for member in existing
    ]
    if len(existing) >= target:
        return cohort
    seen = {member.agent_id for member in cohort}
    for member in await historical_rescore_cohort(
        session,
        source_version=rollout.from_version,
        limit=target,
    ):
        if member.agent_id in seen:
            continue
        cohort.append(member)
        seen.add(member.agent_id)
        if len(cohort) == target:
            break
    return cohort


async def expand_rolling_qualification(
    session: AsyncSession,
    *,
    generator: DatasetGenerator,
    now: datetime,
    desired_version: int,
    expected_active_version: int,
    expected_current_target: int,
    new_target: int,
    actor: str,
    reason: str,
) -> RolloutExpansionResult:
    """Explicitly expand one open rollout after rendering every new dataset.

    Unlike changing the queue-policy default, this action names and guards the
    already-open rollout. Dataset generation happens before the rollout row is
    changed; the second transaction rechecks every input and either appends the
    full ordered suffix or rolls back without moving the target.
    """
    if not PRIORITY_COHORT_SIZE <= expected_current_target < new_target:
        raise RolloutConflictError(
            "rollout expansion must increase the current target within the "
            f"top-{MAX_PERSISTED_RESCORE_COHORT_SIZE} bound"
        )
    if new_target > MAX_PERSISTED_RESCORE_COHORT_SIZE:
        raise RolloutConflictError(
            f"rollout expansion target {new_target} exceeds "
            f"{MAX_PERSISTED_RESCORE_COHORT_SIZE}"
        )

    pending: list[PendingQualification] = []
    async with session.begin():
        active_version = await active_bench_version(session)
        if active_version != expected_active_version:
            raise RolloutConflictError(
                "active benchmark changed: expected "
                f"v{expected_active_version}, found v{active_version}"
            )
        rollout = await open_rollout(session)
        if rollout is None or rollout.desired_version != desired_version:
            raise RolloutConflictError(
                f"no open benchmark v{desired_version} rollout is available"
            )
        if rollout.rescore_cohort_target != expected_current_target:
            raise RolloutConflictError(
                "rollout rescore target changed: expected "
                f"{expected_current_target}, found {rollout.rescore_cohort_target}"
            )
        cohort = await _rollout_rescore_cohort(
            session, rollout=rollout, target_override=new_target
        )
        if len(cohort) != new_target:
            raise RolloutConflictError(
                f"benchmark v{desired_version} has only {len(cohort)} qualified "
                f"distinct historical members for a target of {new_target}"
            )
        existing_ids = set(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id).where(
                    BenchmarkRolloutMember.rollout_id == rollout.rollout_id
                )
            )
        )
        for member in cohort:
            if member.agent_id in existing_ids:
                continue
            candidate, blocker = await qualification_candidate(
                session,
                source_bench_version=rollout.from_version,
                target_bench_version=rollout.desired_version,
                member=member,
                generator_run_size=generator.run_size,
            )
            if candidate is None:
                raise RolloutConflictError(
                    f"cohort agent {member.agent_id} cannot qualify for "
                    f"v{desired_version}: {blocker}"
                )
            pending.append(candidate)
        if len(existing_ids) + len(pending) != new_target:
            raise RolloutConflictError(
                "rollout membership changed while expansion was prepared"
            )
        rollout_id = rollout.rollout_id

    rendered = [
        (
            candidate,
            candidate.existing_sha256
            or await generator.generate(candidate.seed, bench_version=desired_version),
        )
        for candidate in pending
    ]

    async with session.begin():
        current_active_version = await active_bench_version(session)
        if current_active_version != expected_active_version:
            raise RolloutConflictError(
                "active benchmark changed while expansion datasets were rendered: "
                f"expected v{expected_active_version}, found v{current_active_version}"
            )
        rollout = await session.get(BenchmarkRollout, rollout_id, with_for_update=True)
        if (
            rollout is None
            or rollout.status not in ("collecting", "blocked_ineligible")
            or rollout.desired_version != desired_version
            or rollout.rescore_cohort_target != expected_current_target
        ):
            raise RolloutConflictError(
                "open rollout changed while expansion datasets were rendered"
            )
        current_cohort = {
            member.agent_id: member
            for member in await _rollout_rescore_cohort(
                session, rollout=rollout, target_override=new_target
            )
        }
        if len(current_cohort) != new_target:
            raise RolloutConflictError(
                "rollout ranking changed while expansion datasets were rendered"
            )
        for candidate, _sha256 in rendered:
            current_member = current_cohort.get(candidate.member.agent_id)
            if current_member is None:
                raise RolloutConflictError(
                    "rollout ranking changed while expansion datasets were rendered"
                )
            current_candidate, _blocker = await qualification_candidate(
                session,
                source_bench_version=rollout.from_version,
                target_bench_version=rollout.desired_version,
                member=current_member,
                generator_run_size=generator.run_size,
            )
            if current_candidate != candidate:
                raise RolloutConflictError(
                    "rollout qualification changed while expansion datasets "
                    "were rendered"
                )

        rollout.rescore_cohort_target = new_target
        appended = 0
        for candidate, sha256 in rendered:
            appended += await append_rollout_member(
                session,
                rollout=rollout,
                member=candidate.member,
                dataset=DatasetPin(
                    seed=candidate.seed,
                    sha256=sha256,
                    run_size=candidate.run_size,
                    seed_block=candidate.seed_block,
                    seed_block_hash=candidate.seed_block_hash,
                ),
                now=now,
                audit_context={
                    "origin": "operator_expansion",
                    "actor": actor,
                    "reason": reason,
                    "previous_rescore_cohort_target": expected_current_target,
                    "new_rescore_cohort_target": new_target,
                    "seed_source": candidate.seed_source,
                },
            )
        if appended != len(rendered):
            raise RolloutConflictError(
                "rollout membership changed while expansion was committed"
            )
        rollout.cohort_size = new_target
        await append_rollout_audit(
            session,
            rollout,
            "cohort_expanded",
            {
                "origin": "operator",
                "actor": actor,
                "reason": reason,
                "previous_rescore_cohort_target": expected_current_target,
                "new_rescore_cohort_target": new_target,
                "appended_members": appended,
            },
            now=now,
        )
        await session.flush()

    return RolloutExpansionResult(
        previous_target=expected_current_target,
        new_target=new_target,
        appended_members=len(rendered),
    )


async def qualification_candidate(
    session: AsyncSession,
    *,
    source_bench_version: int,
    target_bench_version: int,
    member: RolloutSnapshotMember,
    generator_run_size: str | None,
) -> tuple[PendingQualification | None, str | None]:
    """Resolve a target-version input without rewriting an older dataset pin.

    Pre-pin agents may have null compatibility columns and multiple accepted
    source-version score seeds. Choosing the numerically smallest distinct seed
    is deterministic across validators, retries, and operators, so no caller
    can cherry-pick the target dataset. The separately versioned pin preserves every
    historical score and never rewrites the legacy compatibility columns.
    """
    agent = await session.get(Agent, member.agent_id)
    assert agent is not None
    versioned = await session.get(
        BenchmarkDataset, (member.agent_id, target_bench_version)
    )
    if versioned is not None:
        return (
            PendingQualification(
                member=member,
                seed=versioned.seed,
                run_size=versioned.run_size,
                seed_block=versioned.seed_block,
                seed_block_hash=versioned.seed_block_hash,
                seed_source="versioned_pin",
                existing_sha256=versioned.sha256,
            ),
            None,
        )
    if (
        agent.dataset_seed is not None
        and agent.dataset_sha256 is not None
        and agent.dataset_run_size is not None
    ):
        return (
            PendingQualification(
                member=member,
                seed=agent.dataset_seed,
                run_size=agent.dataset_run_size,
                seed_block=agent.dataset_seed_block,
                seed_block_hash=agent.dataset_seed_block_hash,
                seed_source="legacy_pin",
            ),
            None,
        )

    score_rows = (
        await session.execute(
            select(Score.bench_version, Score.seed)
            .where(
                Score.agent_id == member.agent_id,
                Score.bench_version <= source_bench_version,
            )
            .order_by(Score.bench_version.desc(), Score.seed.asc())
        )
    ).all()
    latest_score_version = score_rows[0][0] if score_rows else None
    score_seeds = {
        seed for version, seed in score_rows if version == latest_score_version
    }
    if score_seeds:
        seed = min(score_seeds)
        seed_source: Literal[
            "legacy_pin",
            "source_scores_canonical_min",
            "historical_scores_latest_min",
        ] = (
            "source_scores_canonical_min"
            if latest_score_version == source_bench_version
            else "historical_scores_latest_min"
        )
        seed_block = None
        seed_block_hash = None
    elif agent.dataset_seed is not None:
        seed = agent.dataset_seed
        seed_source = "legacy_pin"
        seed_block = agent.dataset_seed_block
        seed_block_hash = agent.dataset_seed_block_hash
    else:
        return None, "missing_dataset_seed"
    run_size = agent.dataset_run_size or generator_run_size
    if run_size is None:
        return None, "dataset_generator_disabled"
    return (
        PendingQualification(
            member=member,
            seed=seed,
            run_size=run_size,
            seed_block=seed_block,
            seed_block_hash=seed_block_hash,
            seed_source=seed_source,
        ),
        None,
    )


async def rolling_qualification_blockers(
    session: AsyncSession, *, generator_run_size: str | None
) -> list[dict[str, str]]:
    """Describe inherited cohort agents that automatic qualification cannot add."""
    rollout = await open_rollout(session)
    if rollout is None:
        return []
    blockers: list[dict[str, str]] = []
    for member in await _rollout_rescore_cohort(session, rollout=rollout):
        if (
            await session.get(
                BenchmarkRolloutMember, (rollout.rollout_id, member.agent_id)
            )
            is not None
        ):
            continue
        candidate, reason = await qualification_candidate(
            session,
            source_bench_version=rollout.from_version,
            target_bench_version=rollout.desired_version,
            member=member,
            generator_run_size=generator_run_size,
        )
        if candidate is None:
            assert reason is not None
            blockers.append({"agent_id": str(member.agent_id), "reason": reason})
    return blockers


async def ensure_rolling_qualification(
    session: AsyncSession, *, generator: DatasetGenerator, now: datetime
) -> bool:
    """Compatibility no-op: rollout creation is an authenticated admin action.

    Older internal callers may still import this helper during an asynchronous
    deploy. Keeping it as a fail-closed no-op avoids an import failure without
    allowing a heartbeat or validator job poll to activate a shipped contract.
    """
    del session, generator, now
    return False


async def _select_carryover(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    generator_run_size: str | None,
    now: datetime,
) -> list[PendingCarryover]:
    """Phase one of the carryover pass: pick adoptees and resolve their seeds.

    The operator gate is checked first and nothing else runs when it is off, so
    the shipped default costs exactly one settings SELECT and leaves every query
    below unexecuted. Blocked candidates are logged the way the cohort pass logs
    its own, since the reasons are the same typed reasons from
    :func:`qualification_candidate`.
    """
    settings = (await resolve_queue_policy_settings(session)).prev_gen_carryover
    if not settings.enabled:
        return []
    selected: list[PendingCarryover] = []
    for candidate in await stranded_prev_gen_candidates(
        session, rollout=rollout, settings=settings, now=now
    ):
        qualification, reason = await qualification_candidate(
            session,
            source_bench_version=rollout.from_version,
            target_bench_version=rollout.desired_version,
            member=RolloutSnapshotMember(
                candidate.agent_id, candidate.miner_hotkey, 0.0
            ),
            generator_run_size=generator_run_size,
        )
        if qualification is None:
            logger.warning(
                "benchmark carryover blocked agent_id=%s reason=%s",
                candidate.agent_id,
                reason,
            )
            continue
        selected.append(
            PendingCarryover(candidate=candidate, qualification=qualification)
        )
    return selected


async def _adopt_carryover(
    session: AsyncSession,
    *,
    rollout: BenchmarkRollout,
    rendered: list[tuple[PendingCarryover, str]],
    now: datetime,
) -> int:
    """Phase three: pin each rendered dataset and its admission row together.

    Re-resolves the seed under the rollout lock and skips any candidate whose
    input moved while the generator was running, exactly like the cohort pass, so
    a concurrent refresh can never pin a dataset that no longer matches. The
    carryover row and the dataset are written by one call
    (:func:`adopt_carryover_agent`) inside this transaction, which is what makes
    admission unable to outrun generation.
    """
    adopted = 0
    for adoption, sha256 in rendered:
        current, _reason = await qualification_candidate(
            session,
            source_bench_version=rollout.from_version,
            target_bench_version=rollout.desired_version,
            member=adoption.qualification.member,
            generator_run_size=adoption.qualification.run_size,
        )
        if current != adoption.qualification:
            logger.warning(
                "benchmark carryover changed during render agent_id=%s",
                adoption.candidate.agent_id,
            )
            continue
        if await adopt_carryover_agent(
            session,
            rollout=rollout,
            candidate=adoption.candidate,
            dataset=DatasetPin(
                seed=adoption.qualification.seed,
                sha256=sha256,
                run_size=adoption.qualification.run_size,
                seed_block=adoption.qualification.seed_block,
                seed_block_hash=adoption.qualification.seed_block_hash,
            ),
            now=now,
            audit_context={"seed_source": adoption.qualification.seed_source},
        ):
            adopted += 1
    if adopted:
        logger.info(
            "benchmark carryover adopted rollout_id=%s agents=%s",
            rollout.rollout_id,
            adopted,
        )
    return adopted


async def refresh_rolling_qualification(
    session: AsyncSession,
    *,
    generator: DatasetGenerator,
    now: datetime,
    inference_config: InferenceProxyConfig | None = None,
) -> int:
    """Converge the frozen inherited rescore cohort and try activation.

    Dataset rendering deliberately happens between transactions: the generator
    is a network service and must never run while holding rollout/agent locks.
    The second transaction rechecks membership, so concurrent refreshes are
    idempotent.

    Previous-generation carryover converges here too, as a second pass with the
    same three-phase shape rather than a loop of its own -- one convergence loop
    means one place where "selected, rendered, pinned" can be reasoned about. It
    is gated on operator policy and ships disabled, in which case it costs one
    settings SELECT and runs no carryover query at all.

    Returns the number of inherited cohort members appended. Carryover adoptions
    are deliberately excluded from that count: callers read it as cohort
    progress toward activation, and carryover does not move that bar.
    """
    pending: list[PendingQualification] = []
    carryover_pending: list[PendingCarryover] = []
    async with session.begin():
        rollout = await open_rollout(session)
        if rollout is None:
            return 0
        cohort = await _rollout_rescore_cohort(session, rollout=rollout)
        if len(cohort) < PRIORITY_COHORT_SIZE:
            logger.error(
                "benchmark rollout cannot build inherited cohort rollout_id=%s "
                "eligible_members=%s",
                rollout.rollout_id,
                len(cohort),
            )
            return 0
        rollout.cohort_size = len(cohort)
        for member in cohort:
            existing = await session.get(
                BenchmarkRolloutMember, (rollout.rollout_id, member.agent_id)
            )
            if existing is not None:
                continue
            candidate, reason = await qualification_candidate(
                session,
                source_bench_version=rollout.from_version,
                target_bench_version=rollout.desired_version,
                member=member,
                generator_run_size=generator.run_size,
            )
            if candidate is None:
                logger.warning(
                    "benchmark qualification blocked agent_id=%s reason=%s",
                    member.agent_id,
                    reason,
                )
                continue
            pending.append(candidate)
        rollout_id: UUID = rollout.rollout_id
        desired_version = rollout.desired_version
        carryover_pending = await _select_carryover(
            session,
            rollout=rollout,
            generator_run_size=generator.run_size,
            now=now,
        )

    rendered: list[tuple[PendingQualification, str]] = []
    for candidate in pending:
        rendered.append(
            (
                candidate,
                candidate.existing_sha256
                or await generator.generate(
                    candidate.seed, bench_version=desired_version
                ),
            )
        )
    carryover_rendered: list[tuple[PendingCarryover, str]] = []
    for adoption in carryover_pending:
        carryover_rendered.append(
            (
                adoption,
                adoption.qualification.existing_sha256
                or await generator.generate(
                    adoption.qualification.seed, bench_version=desired_version
                ),
            )
        )

    appended = 0
    async with session.begin():
        rollout = await session.get(BenchmarkRollout, rollout_id, with_for_update=True)
        if rollout is None or rollout.status not in (
            "collecting",
            "blocked_ineligible",
        ):
            return 0
        current_cohort = {
            member.agent_id: member
            for member in await _rollout_rescore_cohort(session, rollout=rollout)
        }
        for candidate, sha256 in rendered:
            current_member = current_cohort.get(candidate.member.agent_id)
            if current_member is None:
                continue
            current_candidate, _reason = await qualification_candidate(
                session,
                source_bench_version=rollout.from_version,
                target_bench_version=rollout.desired_version,
                member=current_member,
                generator_run_size=generator.run_size,
            )
            if current_candidate != candidate:
                logger.warning(
                    "benchmark qualification changed during render agent_id=%s",
                    candidate.member.agent_id,
                )
                continue
            appended += await append_rollout_member(
                session,
                rollout=rollout,
                member=candidate.member,
                dataset=DatasetPin(
                    seed=candidate.seed,
                    sha256=sha256,
                    run_size=candidate.run_size,
                    seed_block=candidate.seed_block,
                    seed_block_hash=candidate.seed_block_hash,
                ),
                now=now,
                audit_context={"seed_source": candidate.seed_source},
            )
        await _adopt_carryover(
            session, rollout=rollout, rendered=carryover_rendered, now=now
        )
        await maybe_activate_rollout(
            session,
            rollout,
            now=now,
            inference_requirements=inference_activation_requirements(
                inference_config, bench_version=rollout.desired_version
            ),
        )
    return appended
