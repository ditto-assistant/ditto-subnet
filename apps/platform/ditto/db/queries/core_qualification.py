"""Append-only shadow core qualification policies and observations."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select

from ditto.api_models.core_qualification import (
    CoreQualificationPolicy,
    CoreQualificationPolicyRevision,
    core_qualification_policy_checksum,
)
from ditto.db.models import (
    Agent,
    CoreQualificationObservation,
    Score,
)
from ditto.db.models import (
    CoreQualificationPolicyRevision as CoreQualificationPolicyRevisionRow,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, SCORING_QUORUM

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CoreQualificationObservationResult:
    row: CoreQualificationObservation
    idempotent: bool


def policy_from_row(
    row: CoreQualificationPolicyRevisionRow,
) -> CoreQualificationPolicy:
    return CoreQualificationPolicy(
        schema="ditto-core-qualification-policy-v1",
        weight_eligible=False,
        bench_version=row.bench_version,
        enter_composite=row.enter_composite,
        enter_tool_mean=row.enter_tool_mean,
        enter_memory_mean=row.enter_memory_mean,
        exit_composite=row.exit_composite,
        exit_tool_mean=row.exit_tool_mean,
        exit_memory_mean=row.exit_memory_mean,
        enter_observations=row.enter_observations,
        exit_observations=row.exit_observations,
    )


def policy_revision_from_row(
    row: CoreQualificationPolicyRevisionRow,
) -> CoreQualificationPolicyRevision:
    return CoreQualificationPolicyRevision(
        revision=row.revision,
        parent_revision=row.parent_revision,
        policy=policy_from_row(row),
        checksum=row.checksum,
        reason=row.reason,
        actor=row.actor,
        created_at=row.created_at,
    )


async def latest_core_qualification_policy(
    session: AsyncSession,
    *,
    bench_version: int,
    for_update: bool = False,
) -> CoreQualificationPolicyRevisionRow | None:
    statement = (
        select(CoreQualificationPolicyRevisionRow)
        .where(CoreQualificationPolicyRevisionRow.bench_version == bench_version)
        .order_by(CoreQualificationPolicyRevisionRow.revision.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def list_core_qualification_policies(
    session: AsyncSession,
    *,
    bench_version: int,
    limit: int = 100,
) -> Sequence[CoreQualificationPolicyRevisionRow]:
    return list(
        await session.scalars(
            select(CoreQualificationPolicyRevisionRow)
            .where(CoreQualificationPolicyRevisionRow.bench_version == bench_version)
            .order_by(CoreQualificationPolicyRevisionRow.revision.desc())
            .limit(limit)
        )
    )


async def insert_core_qualification_policy(
    session: AsyncSession,
    *,
    parent_revision: int,
    policy: CoreQualificationPolicy,
    reason: str,
    actor: str,
) -> CoreQualificationPolicyRevisionRow:
    checksum = core_qualification_policy_checksum(policy)
    row = CoreQualificationPolicyRevisionRow(
        bench_version=policy.bench_version,
        parent_revision=parent_revision,
        enter_composite=policy.enter_composite,
        enter_tool_mean=policy.enter_tool_mean,
        enter_memory_mean=policy.enter_memory_mean,
        exit_composite=policy.exit_composite,
        exit_tool_mean=policy.exit_tool_mean,
        exit_memory_mean=policy.exit_memory_mean,
        enter_observations=policy.enter_observations,
        exit_observations=policy.exit_observations,
        weight_eligible=False,
        checksum=checksum,
        reason=reason.strip(),
        actor=actor.strip(),
    )
    session.add(row)
    await session.flush()
    return row


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _score_projection(
    *,
    agent: Agent,
    bench_version: int,
    scores: Sequence[Score],
) -> dict:
    return {
        "schema": "ditto-core-score-evidence-v1",
        "agent_id": str(agent.agent_id),
        "artifact_sha256": agent.sha256,
        "screened_image_sha256": agent.screened_image_sha256,
        "bench_version": bench_version,
        "scores": [
            {
                "validator_hotkey": score.validator_hotkey,
                "run_id": score.run_id,
                "seed": score.seed,
                "composite": score.composite,
                "tool_mean": score.tool_mean,
                "memory_mean": score.memory_mean,
                "n": score.n,
                "generated_at": _aware(score.generated_at).isoformat(
                    timespec="microseconds"
                ),
                "signature": score.signature,
            }
            for score in scores
        ],
    }


def _evidence_digest(evidence: dict) -> str:
    body = (
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return hashlib.sha256(body).hexdigest()


async def latest_core_qualification_observation(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    policy_revision: int,
) -> CoreQualificationObservation | None:
    return await session.scalar(
        select(CoreQualificationObservation)
        .where(
            CoreQualificationObservation.agent_id == agent_id,
            CoreQualificationObservation.artifact_sha256 == artifact_sha256,
            CoreQualificationObservation.screened_image_sha256 == screened_image_sha256,
            CoreQualificationObservation.bench_version == bench_version,
            CoreQualificationObservation.policy_revision == policy_revision,
        )
        .order_by(
            CoreQualificationObservation.sequence.desc(),
        )
        .limit(1)
    )


async def _latest_complete_core_qualification_observation(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    policy_revision: int,
) -> CoreQualificationObservation | None:
    return await session.scalar(
        select(CoreQualificationObservation)
        .where(
            CoreQualificationObservation.agent_id == agent_id,
            CoreQualificationObservation.artifact_sha256 == artifact_sha256,
            CoreQualificationObservation.screened_image_sha256 == screened_image_sha256,
            CoreQualificationObservation.bench_version == bench_version,
            CoreQualificationObservation.policy_revision == policy_revision,
            CoreQualificationObservation.complete_wave.is_(True),
        )
        .order_by(
            CoreQualificationObservation.sequence.desc(),
        )
        .limit(1)
    )


async def observe_core_qualification(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    now: datetime,
    source: str = "score_commit",
    actor: str | None = None,
    reason: str | None = None,
) -> CoreQualificationObservationResult | None:
    """Record one idempotent score snapshot without touching score state."""

    policy_row = await latest_core_qualification_policy(
        session, bench_version=bench_version
    )
    if policy_row is None:
        return None
    if source not in {"score_commit", "admin_refresh"}:
        raise ValueError("unknown core qualification observation source")
    if source == "score_commit" and (actor is not None or reason is not None):
        raise ValueError("automatic observation cannot carry an operator identity")
    if source == "admin_refresh":
        actor = (actor or "").strip()
        reason = (reason or "").strip()
        if not actor or len(actor) > 120 or len(reason) < 8:
            raise ValueError("admin refresh requires a bounded actor and reason")
    policy = policy_from_row(policy_row)
    agent = await session.get(Agent, agent_id, with_for_update=True)
    if agent is None or agent.screened_image_sha256 is None:
        return None
    scores = list(
        (
            await session.scalars(
                select(Score)
                .where(
                    Score.agent_id == agent_id,
                    Score.bench_version == bench_version,
                )
                .order_by(Score.validator_hotkey)
            )
        ).all()
    )
    if len(scores) < SCORING_QUORUM:
        return None
    numeric = [
        value
        for score in scores
        for value in (score.composite, score.tool_mean, score.memory_mean)
    ]
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("core qualification score evidence is not finite")

    evidence = _score_projection(
        agent=agent,
        bench_version=bench_version,
        scores=scores,
    )
    evidence_sha256 = _evidence_digest(evidence)
    existing = await session.scalar(
        select(CoreQualificationObservation).where(
            CoreQualificationObservation.agent_id == agent_id,
            CoreQualificationObservation.artifact_sha256 == agent.sha256,
            CoreQualificationObservation.screened_image_sha256
            == agent.screened_image_sha256,
            CoreQualificationObservation.bench_version == bench_version,
            CoreQualificationObservation.policy_revision == policy_row.revision,
            CoreQualificationObservation.score_evidence_sha256 == evidence_sha256,
        )
    )
    if existing is not None:
        return CoreQualificationObservationResult(row=existing, idempotent=True)

    full_size = all(score.n >= MIN_ELIGIBLE_CASES for score in scores)
    median_composite = float(median(score.composite for score in scores))
    median_tool_mean = float(median(score.tool_mean for score in scores))
    median_memory_mean = float(median(score.memory_mean for score in scores))
    entry_passed = full_size and (
        median_composite >= policy.enter_composite
        and median_tool_mean >= policy.enter_tool_mean
        and median_memory_mean >= policy.enter_memory_mean
    )
    retention_passed = full_size and (
        median_composite >= policy.exit_composite
        and median_tool_mean >= policy.exit_tool_mean
        and median_memory_mean >= policy.exit_memory_mean
    )
    previous = await latest_core_qualification_observation(
        session,
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy_row.revision,
    )
    wave_baseline = await _latest_complete_core_qualification_observation(
        session,
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy_row.revision,
    )
    current_runs = {score.validator_hotkey: score.run_id for score in scores}
    previous_scores = (
        wave_baseline.score_evidence.get("scores", [])
        if wave_baseline is not None and isinstance(wave_baseline.score_evidence, dict)
        else []
    )
    previous_runs = {
        str(item.get("validator_hotkey")): str(item.get("run_id"))
        for item in previous_scores
        if isinstance(item, dict)
    }
    complete_wave = wave_baseline is None or all(
        current_runs.get(validator) != run_id
        for validator, run_id in previous_runs.items()
    )
    if previous is not None and not complete_wave:
        qualified = previous.qualified
        enter_streak = previous.enter_streak
        exit_streak = previous.exit_streak
        decision = "partial_wave"
    elif previous is not None and previous.qualified:
        enter_streak = 0
        exit_streak = 0 if retention_passed else previous.exit_streak + 1
        qualified = retention_passed or exit_streak < policy.exit_observations
        decision = (
            "held" if retention_passed else ("pending_exit" if qualified else "exited")
        )
    else:
        exit_streak = 0
        enter_streak = (
            (previous.enter_streak if previous is not None else 0) + 1
            if entry_passed
            else 0
        )
        qualified = entry_passed and enter_streak >= policy.enter_observations
        decision = (
            "entered"
            if qualified
            else ("pending_entry" if entry_passed else "below_entry")
        )

    row = CoreQualificationObservation(
        observation_id=uuid4(),
        agent_id=agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy_row.revision,
        policy_checksum=policy_row.checksum,
        score_evidence_sha256=evidence_sha256,
        score_count=len(scores),
        full_size=full_size,
        complete_wave=complete_wave,
        score_evidence=evidence,
        median_composite=median_composite,
        median_tool_mean=median_tool_mean,
        median_memory_mean=median_memory_mean,
        entry_passed=entry_passed,
        retention_passed=retention_passed,
        qualified=qualified,
        enter_streak=enter_streak,
        exit_streak=exit_streak,
        decision=decision,
        source=source,
        actor=actor,
        reason=reason,
        weight_eligible=False,
        observed_at=_aware(now),
    )
    session.add(row)
    await session.flush()
    return CoreQualificationObservationResult(row=row, idempotent=False)


async def list_agent_core_qualification_observations(
    session: AsyncSession,
    *,
    agent_id: UUID,
    limit: int,
) -> tuple[list[CoreQualificationObservation], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CoreQualificationObservation)
            .where(CoreQualificationObservation.agent_id == agent_id)
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(CoreQualificationObservation)
                .where(CoreQualificationObservation.agent_id == agent_id)
                .order_by(
                    CoreQualificationObservation.sequence.desc(),
                )
                .limit(limit)
            )
        ).all()
    )
    return rows, total
