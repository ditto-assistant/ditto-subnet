"""Issue, claim, and abort shadow coding-certification leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseStatus,
)
from ditto.api_server.coding_certification_canary import (
    CodingCertificationCanaryUnavailableError,
    public_certification_canary,
)
from ditto.db.models import Agent, CodingCertificationLease
from ditto.db.queries.core_qualification import (
    latest_complete_core_qualification_observation,
    latest_core_qualification_policy,
    lock_core_qualification_bench,
)

_LEASE_TTL = timedelta(minutes=20)
_INFLIGHT = (
    CodingCertificationLeaseStatus.ISSUED.value,
    CodingCertificationLeaseStatus.CLAIMED.value,
)


class CodingCertificationLeaseNotAvailableError(RuntimeError):
    """The agent is not currently eligible for a certification lease."""


class CodingCertificationLeaseConflictError(RuntimeError):
    """An in-flight lease already exists, or the requested transition is illegal."""


class CodingCertificationLeaseUnavailableError(RuntimeError):
    """The public canary identity or database clock is unavailable."""


@dataclass(frozen=True)
class CodingCertificationLeaseResult:
    row: CodingCertificationLease
    authority: CodingCertificationLeaseAuthority
    idempotent: bool


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _database_now(session: AsyncSession) -> datetime:
    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):  # pragma: no cover - DB invariant
        raise CodingCertificationLeaseUnavailableError(
            "database clock did not return a timestamp"
        )
    return _aware(now)


def _screened_image_is_complete(agent: Agent) -> bool:
    return (
        agent.screened_image_sha256 is not None
        and len(agent.screened_image_sha256) == 64
        and agent.screened_image_size_bytes is not None
        and agent.screened_image_size_bytes > 0
        and agent.screened_image_id is not None
        and agent.screened_image_ref is not None
        and agent.screened_image_upload_id is not None
        and agent.screened_image_verified_at is not None
    )


def authority_from_row(
    row: CodingCertificationLease,
) -> CodingCertificationLeaseAuthority:
    return CodingCertificationLeaseAuthority.model_validate(row.authority)


def result_from_row(
    row: CodingCertificationLease, *, idempotent: bool
) -> CodingCertificationLeaseResult:
    return CodingCertificationLeaseResult(
        row=row,
        authority=authority_from_row(row),
        idempotent=idempotent,
    )


async def _expire_due_leases(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    coding_contract_version: int,
    now: datetime,
) -> None:
    rows = (
        await session.scalars(
            select(CodingCertificationLease)
            .where(
                CodingCertificationLease.agent_id == agent_id,
                CodingCertificationLease.artifact_sha256 == artifact_sha256,
                CodingCertificationLease.screened_image_sha256 == screened_image_sha256,
                CodingCertificationLease.bench_version == bench_version,
                CodingCertificationLease.coding_contract_version
                == coding_contract_version,
                CodingCertificationLease.status.in_(_INFLIGHT),
            )
            .with_for_update()
        )
    ).all()
    for row in rows:
        if (
            _aware(row.deadline) <= now
            and row.status == CodingCertificationLeaseStatus.ISSUED.value
        ):
            row.status = CodingCertificationLeaseStatus.EXPIRED.value
    await session.flush()


async def issue_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    coding_contract_version: int = 1,
) -> CodingCertificationLeaseResult:
    """Mint one canary lease if current core qualification still holds."""

    if coding_contract_version != 1:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease contract is not available"
        )
    agent = await session.get(Agent, agent_id, with_for_update=True)
    if agent is None or not _screened_image_is_complete(agent):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    assert agent.screened_image_sha256 is not None
    assert agent.screened_image_id is not None
    assert agent.screened_image_ref is not None
    assert agent.screened_image_upload_id is not None
    await lock_core_qualification_bench(session, bench_version=bench_version)
    policy = await latest_core_qualification_policy(
        session, bench_version=bench_version
    )
    if policy is None:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    observation = await latest_complete_core_qualification_observation(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        policy_revision=policy.revision,
    )
    if (
        observation is None
        or not observation.qualified
        or observation.policy_checksum != policy.checksum
        or observation.weight_eligible
    ):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    now = await _database_now(session)
    await _expire_due_leases(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        coding_contract_version=coding_contract_version,
        now=now,
    )
    inflight = await session.scalar(
        select(CodingCertificationLease)
        .where(
            CodingCertificationLease.agent_id == agent.agent_id,
            CodingCertificationLease.artifact_sha256 == agent.sha256,
            CodingCertificationLease.screened_image_sha256
            == agent.screened_image_sha256,
            CodingCertificationLease.bench_version == bench_version,
            CodingCertificationLease.coding_contract_version == coding_contract_version,
            CodingCertificationLease.status.in_(_INFLIGHT),
        )
        .with_for_update()
        .limit(1)
    )
    if inflight is not None:
        if (
            inflight.validator_hotkey == validator_hotkey
            and inflight.status == CodingCertificationLeaseStatus.ISSUED.value
        ):
            return result_from_row(inflight, idempotent=True)
        raise CodingCertificationLeaseConflictError(
            "coding certification lease already exists for this artifact"
        )
    try:
        canary = public_certification_canary()
    except CodingCertificationCanaryUnavailableError as error:
        raise CodingCertificationLeaseUnavailableError(str(error)) from error
    lease_id = uuid4()
    deadline = now + _LEASE_TTL
    authority = CodingCertificationLeaseAuthority(
        schema="dittobench-coding-certification-lease-v1",
        coding_contract_version=1,
        weight_eligible=False,
        lease_id=lease_id,
        validator_hotkey=validator_hotkey,
        agent_id=agent.agent_id,
        agent_artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        core_qualification_observation_id=observation.observation_id,
        core_qualification_policy_checksum=observation.policy_checksum,
        canary_manifest_sha256=canary.canary_manifest_sha256,
        runner_plan_sha256=canary.runner_plan_sha256,
        grader_plan_sha256=canary.grader_plan_sha256,
        resource_profile_sha256=canary.resource_profile_sha256,
        inference_policy_sha256=canary.inference_policy_sha256,
        issued_at=now,
        deadline=deadline,
    )
    row = CodingCertificationLease(
        lease_id=lease_id,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        screened_image_upload_id=agent.screened_image_upload_id,
        validator_hotkey=validator_hotkey,
        bench_version=bench_version,
        coding_contract_version=1,
        core_qualification_observation_id=observation.observation_id,
        core_qualification_policy_checksum=observation.policy_checksum,
        canary_manifest_sha256=canary.canary_manifest_sha256,
        runner_plan_sha256=canary.runner_plan_sha256,
        grader_plan_sha256=canary.grader_plan_sha256,
        resource_profile_sha256=canary.resource_profile_sha256,
        inference_policy_sha256=canary.inference_policy_sha256,
        status=CodingCertificationLeaseStatus.ISSUED.value,
        weight_eligible=False,
        issued_at=now,
        deadline=deadline,
        authority=authority.model_dump(mode="json", by_alias=True),
    )
    session.add(row)
    await session.flush()
    return result_from_row(row, idempotent=False)


async def claim_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    lease_id: UUID,
) -> CodingCertificationLeaseResult:
    """Exclusive claim of an issued lease by the named validator."""

    now = await _database_now(session)
    row = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    if row is None or row.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if (
        row.status == CodingCertificationLeaseStatus.ISSUED.value
        and _aware(row.deadline) <= now
    ):
        row.status = CodingCertificationLeaseStatus.EXPIRED.value
        await session.flush()
        return result_from_row(row, idempotent=False)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        return result_from_row(row, idempotent=True)
    if row.status != CodingCertificationLeaseStatus.ISSUED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    row.status = CodingCertificationLeaseStatus.CLAIMED.value
    row.claimed_at = now
    await session.flush()
    return result_from_row(row, idempotent=False)


async def abort_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    lease_id: UUID,
) -> CodingCertificationLeaseResult:
    """Abort an unclaimed issued lease. Claimed leases cannot clean-rerun."""

    now = await _database_now(session)
    row = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    if row is None or row.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if row.status == CodingCertificationLeaseStatus.ABORTED.value:
        return result_from_row(row, idempotent=True)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        raise CodingCertificationLeaseConflictError(
            "claimed coding certification lease cannot be aborted"
        )
    if (
        row.status == CodingCertificationLeaseStatus.ISSUED.value
        and _aware(row.deadline) <= now
    ):
        row.status = CodingCertificationLeaseStatus.EXPIRED.value
        await session.flush()
        return result_from_row(row, idempotent=False)
    if row.status != CodingCertificationLeaseStatus.ISSUED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    row.status = CodingCertificationLeaseStatus.ABORTED.value
    row.aborted_at = now
    await session.flush()
    return result_from_row(row, idempotent=False)


async def get_coding_certification_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
) -> CodingCertificationLease | None:
    return await session.get(CodingCertificationLease, lease_id)
