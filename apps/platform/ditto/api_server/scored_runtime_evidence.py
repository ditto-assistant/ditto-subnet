"""Bind signed validator scorer probes to one exact screening lease.

No single validator can describe the environment for a submission whose future
scorer has not been chosen. A packet is available only while every V13
validator eligible for that work reports the same descriptor-bound evidence:
only pinned members signing the cohort's authority packet are issued V13 work,
and at least a quorum of them must be routable. ``validator_count`` is how many
routable members sign it, never fewer than the quorum of three.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.v13_scorer_cohort import (
    pinned_cohort_packet,
    report_only_current_cohort_packet,
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
    report_only_current_packet: bool = False,
) -> ScoredRuntimeEvidenceLease | None:
    if policy_version != 13 or bench_version != 13:
        return None
    now = now or datetime.now(UTC)
    if report_only_current_packet:
        pinned = await report_only_current_cohort_packet(session, now=now)
    else:
        pinned = await pinned_cohort_packet(session, now=now)
    if pinned is None:
        return None
    packet = pinned.packet
    return ScoredRuntimeEvidenceLease(
        attempt_id=attempt_id,
        artifact_sha256=artifact_sha256,
        policy_version=13,
        bench_version=13,
        scorer_source_revision=packet.source_revision,
        release_descriptor_digest=packet.release_descriptor_digest,
        scorer_image_digest=packet.scorer_image_digest,
        scorer_env_sha256=packet.scorer_env_sha256,
        injected_keys=packet.injected_keys,
        validator_count=len(pinned.routable),
        observed_at=pinned.oldest_scorer_observed_at,
    )
