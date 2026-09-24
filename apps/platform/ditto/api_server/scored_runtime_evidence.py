"""Bind signed validator scorer probes to one exact screening lease.

No single validator can describe the environment for a submission whose future
scorer has not been chosen. A packet is available only while every fresh V13
validator eligible for that work reports the same descriptor-bound evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.validator_capabilities import ValidatorStackIdentity
from ditto.db.models import ValidatorHeartbeat
from ditto.db.queries.benchmark_rollout import (
    heartbeat_supports_version,
    verified_scorer_for_version,
)
from ditto_screening_protocol import ScoredRuntimeEvidenceLease


async def scored_runtime_evidence_for_lease(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    artifact_sha256: str,
    policy_version: int,
    bench_version: int,
    now: datetime | None = None,
) -> ScoredRuntimeEvidenceLease | None:
    if policy_version != 13 or bench_version != 13:
        return None
    now = now or datetime.now(UTC)
    heartbeats = (await session.scalars(select(ValidatorHeartbeat))).all()
    eligible = [
        row
        for row in heartbeats
        if heartbeat_supports_version(row, now=now, version=13)
    ]
    if not 1 <= len(eligible) <= 1_000:
        return None
    identity: tuple[str, str, str, str, tuple[str, ...]] | None = None
    oldest_observation: int | None = None
    for row in eligible:
        try:
            stack = ValidatorStackIdentity.model_validate_json(json.dumps(row.stack))
        except ValidationError:
            return None
        if stack.mode != "managed" or stack.release_descriptor_digest is None:
            return None
        scorer_component = stack.components.dittobench_api
        if scorer_component.image_digest is None:
            return None
        scorer = verified_scorer_for_version(row, version=13)
        packet = scorer.scored_runtime_env if scorer is not None else None
        if packet is None or scorer is None or scorer.observed_at is None:
            return None
        current = (
            packet.source_revision,
            stack.release_descriptor_digest,
            scorer_component.image_digest,
            packet.sha256,
            packet.injected_keys,
        )
        if identity is not None and identity != current:
            return None
        identity = current
        oldest_observation = min(
            oldest_observation or scorer.observed_at, scorer.observed_at
        )
    assert identity is not None and oldest_observation is not None
    return ScoredRuntimeEvidenceLease(
        attempt_id=attempt_id,
        artifact_sha256=artifact_sha256,
        policy_version=13,
        bench_version=13,
        scorer_source_revision=identity[0],
        release_descriptor_digest=identity[1],
        scorer_image_digest=identity[2],
        scorer_env_sha256=identity[3],
        injected_keys=identity[4],
        validator_count=len(eligible),
        observed_at=oldest_observation,
    )
