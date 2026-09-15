"""Issue, claim, abort, and audit shadow coding-certification leases.

Every deadline decision reads the database clock after the relevant row locks
are held. Claim, harness launch, inference grants, and receipts share one
authority gate, :func:`lock_coding_certification_lease`, which also enforces
the strict operator allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, func, not_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_leases import (
    CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS,
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseStatus,
)
from ditto.api_server.coding_certification_canary import (
    CodingCertificationCanaryUnavailableError,
    public_certification_canary,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_allowlist import (
    CodingCertificationAllowlist,
    CodingCertificationAllowlistRefusedError,
    active_coding_certification_allowlist,
)
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
# A claimed lease without a receipt releases its identity once its receipt
# window passes, so bound how often one exact identity can be re-run (and
# re-granted Platform-paid inference) inside a rolling window. Only claims an
# allowlist revision admitted count.
MAX_CLAIMED_ATTEMPTS_PER_IDENTITY = 3
CLAIMED_ATTEMPT_WINDOW = timedelta(hours=24)
RECEIPT_GRACE = timedelta(seconds=CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS)
# ``coding_certifications_expiry_check``: a receipt expires within 24 hours of
# its ``issued_at``. Used only for a completed lease whose receipt is missing.
CERTIFICATION_MAX_VALIDITY = timedelta(hours=24)


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


@dataclass(frozen=True)
class CodingCertificationLeaseGate:
    """A locked lease that the current allowlist admits, and the DB time."""

    lease: CodingCertificationLease
    allowlist: CodingCertificationAllowlist
    now: datetime


@dataclass(frozen=True)
class CodingCertificationLeaseAuditRow:
    lease: CodingCertificationLease
    inference_grant_status: str | None
    receipt_status: str | None


@dataclass(frozen=True)
class CodingCertificationLeaseAuditPage:
    rows: list[CodingCertificationLeaseAuditRow]
    total: int
    now: datetime


@dataclass(frozen=True)
class CodingCertificationHarnessAuthority:
    agent_id: UUID
    lease_id: UUID
    deadline: datetime
    bench_version: int
    agent_artifact_sha256: str
    screened_image_sha256: str
    screened_image_size_bytes: int
    screened_image_id: str
    screened_image_ref: str
    screened_image_upload_id: UUID
    screening_policy_version: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def database_now(session: AsyncSession) -> datetime:
    """Database ``clock_timestamp()``; read it after taking the row locks."""

    now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):  # pragma: no cover - DB invariant
        raise CodingCertificationLeaseUnavailableError(
            "database clock did not return a timestamp"
        )
    return _aware(now)


def receipt_window_ends_at(lease: CodingCertificationLease) -> datetime:
    """Last instant (exclusive) at which a claimed lease accepts its receipt."""

    return _aware(lease.deadline) + RECEIPT_GRACE


def lease_is_due(lease: CodingCertificationLease, *, now: datetime) -> bool:
    """Whether an in-flight lease has run out of time on the database clock.

    An issued lease is due at its deadline. A claimed lease is due only when
    its receipt window has passed, because its receipt may still arrive after
    the deadline that already ended harness and inference access. Completed,
    aborted, and expired leases are terminal and never due.
    """

    if lease.status == CodingCertificationLeaseStatus.ISSUED.value:
        return _aware(lease.deadline) <= now
    if lease.status == CodingCertificationLeaseStatus.CLAIMED.value:
        return receipt_window_ends_at(lease) <= now
    return False


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
        await _expire_if_due(session, row, now=now)
    await session.flush()


async def certification_valid_until(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    coding_contract_version: int,
    now: datetime,
) -> datetime | None:
    """When the identity's still-valid certification result expires, else ``None``.

    Contract v1 renews an unchanged identity only after its certification
    expires. The authority is the accepted receipt's own ``expires_at``
    (``coding_capability_certifications``), compared with the caller's database
    time read after the agent row lock, which every receipt write also holds.
    Conservatively, every accepted receipt counts, whatever its status
    (``certified``, ``failed``, or ``unsupported``), settlement binding,
    validator, or lease linkage, so a failed result, a legacy receipt without
    a lease, and a certified result all block issue until they expire, and none
    blocks it after.

    A ``completed`` lease is written only with its receipt. If that receipt is
    ever missing, the lease blocks for the longest validity any receipt for it
    could have had: a receipt's ``issued_at`` is at most the lease deadline and
    ``coding_certifications_expiry_check`` bounds ``expires_at`` to 24 hours
    after it.
    """

    receipt_expiry = await session.scalar(
        select(func.max(CodingCapabilityCertification.expires_at)).where(
            CodingCapabilityCertification.agent_id == agent_id,
            CodingCapabilityCertification.artifact_sha256 == artifact_sha256,
            CodingCapabilityCertification.screened_image_sha256
            == screened_image_sha256,
            CodingCapabilityCertification.bench_version == bench_version,
            CodingCapabilityCertification.coding_contract_version
            == coding_contract_version,
            CodingCapabilityCertification.expires_at > now,
        )
    )
    unreceipted_deadline = await session.scalar(
        select(func.max(CodingCertificationLease.deadline)).where(
            CodingCertificationLease.agent_id == agent_id,
            CodingCertificationLease.artifact_sha256 == artifact_sha256,
            CodingCertificationLease.screened_image_sha256 == screened_image_sha256,
            CodingCertificationLease.bench_version == bench_version,
            CodingCertificationLease.coding_contract_version == coding_contract_version,
            CodingCertificationLease.status
            == CodingCertificationLeaseStatus.COMPLETED.value,
            CodingCertificationLease.deadline > now - CERTIFICATION_MAX_VALIDITY,
            ~select(CodingCapabilityCertification.certification_row_id)
            .where(
                CodingCapabilityCertification.lease_id
                == CodingCertificationLease.lease_id
            )
            .exists(),
        )
    )
    candidates = [_aware(receipt_expiry)] if receipt_expiry is not None else []
    if unreceipted_deadline is not None:
        candidates.append(_aware(unreceipted_deadline) + CERTIFICATION_MAX_VALIDITY)
    return max(candidates, default=None)


async def revoke_live_certification_inference_grants(
    session: AsyncSession, *, lease_id: UUID, now: datetime
) -> int:
    """Terminally revoke a lease's pending or active grant; return how many."""

    grants = (
        await session.scalars(
            select(CodingCertificationInferenceGrant)
            .where(
                CodingCertificationInferenceGrant.lease_id == lease_id,
                CodingCertificationInferenceGrant.status.in_(("pending", "active")),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for grant in grants:
        mark_certification_inference_grant_revoked(grant, now=now)
    return len(grants)


def mark_certification_inference_grant_revoked(
    grant: CodingCertificationInferenceGrant, *, now: datetime
) -> None:
    """Terminal revocation: clears every bearer binding, keeps the accounting."""

    grant.status = "revoked"
    grant.bearer_digest = None
    grant.revoke_bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = now
    grant.updated_at = now


async def _expire_if_due(
    session: AsyncSession,
    row: CodingCertificationLease,
    *,
    now: datetime,
) -> bool:
    """Expire one locked, due in-flight lease and revoke its live grant.

    ``claimed_at`` is kept, so an expired row still shows whether the attempt
    was claimed. A completed (receipted) lease is never due. Nothing is deleted.
    """

    if not lease_is_due(row, now=now):
        return False
    row.status = CodingCertificationLeaseStatus.EXPIRED.value
    await revoke_live_certification_inference_grants(
        session, lease_id=row.lease_id, now=now
    )
    await session.flush()
    return True


async def expire_coding_certification_lease_if_due(
    session: AsyncSession,
    *,
    lease: CodingCertificationLease,
    now: datetime,
) -> bool:
    """Expire a lease the caller already locked, on the caller's database time."""

    return await _expire_if_due(session, lease, now=now)


async def lock_coding_certification_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
    validator_hotkey: str,
) -> CodingCertificationLeaseGate:
    """Shared authority gate for claim, harness launch, grants, and receipts.

    Takes the shared allowlist lock before the lease row lock (the allowlist
    write locks leases after its exclusive lock), then reads the database clock
    after both locks so a lock wait can never make a late request look early.
    Refuses an unknown lease, another validator's lease, or a tuple the current
    allowlist does not admit, before any write.
    """

    allowlist = await active_coding_certification_allowlist(session)
    lease = await session.get(
        CodingCertificationLease,
        lease_id,
        with_for_update=True,
        populate_existing=True,
    )
    now = await database_now(session)
    if lease is None or lease.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if not allowlist.admits(
        agent_id=lease.agent_id,
        artifact_sha256=lease.artifact_sha256,
        screened_image_sha256=lease.screened_image_sha256,
        validator_hotkey=lease.validator_hotkey,
    ):
        raise CodingCertificationAllowlistRefusedError()
    return CodingCertificationLeaseGate(lease=lease, allowlist=allowlist, now=now)


def complete_coding_certification_lease(lease: CodingCertificationLease) -> None:
    """Make a claimed lease terminal in the transaction that accepts its receipt."""

    if lease.status != CodingCertificationLeaseStatus.CLAIMED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    lease.status = CodingCertificationLeaseStatus.COMPLETED.value


def _admitted_tuple_filter(
    allowlist: CodingCertificationAllowlist,
) -> ColumnElement[bool]:
    return tuple_(
        CodingCertificationLease.agent_id,
        CodingCertificationLease.artifact_sha256,
        CodingCertificationLease.screened_image_sha256,
        CodingCertificationLease.validator_hotkey,
    ).in_(
        [
            (UUID(agent_id), artifact_sha256, screened_image_sha256, validator_hotkey)
            for (
                agent_id,
                artifact_sha256,
                screened_image_sha256,
                validator_hotkey,
            ) in sorted(allowlist.tuples)
        ]
    )


async def restamp_admitted_coding_certification_leases(
    session: AsyncSession,
    *,
    allowlist: CodingCertificationAllowlist,
) -> int:
    """Stamp every claimed lease the new revision still admits with that revision.

    The model relay admits paid canary inference only while a claimed lease's
    ``claim_allowlist_revision`` equals the latest allowlist revision. Claims
    stamp the revision that admitted them, and each allowlist write re-stamps
    the claimed leases it still admits, so any other latest revision (none, a
    refuse-all, or a row appended outside this write) refuses at the relay.
    """

    if allowlist.revision < 1 or not allowlist.tuples:
        return 0
    rows = (
        await session.scalars(
            select(CodingCertificationLease)
            .where(
                CodingCertificationLease.status
                == CodingCertificationLeaseStatus.CLAIMED.value,
                _admitted_tuple_filter(allowlist),
            )
            .order_by(CodingCertificationLease.lease_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for row in rows:
        row.claim_allowlist_revision = allowlist.revision
    await session.flush()
    return len(rows)


async def abort_unlisted_coding_certification_leases(
    session: AsyncSession,
    *,
    allowlist: CodingCertificationAllowlist,
) -> int:
    """Abort every in-flight lease the new allowlist refuses; revoke its grants.

    Runs in the allowlist write transaction, after the exclusive allowlist lock
    and the new revision row exist. The tuple filter runs in SQL, so only the
    refused leases are locked. Each aborted lease keeps ``claimed_at`` and names
    the revision that aborted it.
    """

    if allowlist.revision < 1:  # pragma: no cover - a write always has a row
        raise ValueError("coding certification allowlist abort needs a revision")
    statement = select(CodingCertificationLease).where(
        CodingCertificationLease.status.in_(_INFLIGHT)
    )
    if allowlist.tuples:
        statement = statement.where(not_(_admitted_tuple_filter(allowlist)))
    rows = (
        await session.scalars(
            statement.order_by(CodingCertificationLease.lease_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    if not rows:
        return 0
    now = await database_now(session)
    for row in rows:
        row.status = CodingCertificationLeaseStatus.ABORTED.value
        row.aborted_at = now
        row.aborted_allowlist_revision = allowlist.revision
        await revoke_live_certification_inference_grants(
            session, lease_id=row.lease_id, now=now
        )
    await session.flush()
    return len(rows)


async def issue_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    bench_version: int,
    coding_contract_version: int = 1,
) -> CodingCertificationLeaseResult:
    """Mint one canary lease if current core qualification still holds.

    An identity renews: a ``completed`` lease stays terminal, but once every
    certification result for the identity has expired on the database clock a
    fresh lease may be issued, still under the allowlist and the claimed-attempt
    budget. Issue refuses while any lease for the identity is in flight or any
    of its results is still valid (:func:`certification_valid_until`).

    Domain refusals are raised before a lease row is minted. The only writes
    that may precede one are deadline expiry and grant revocation, which the
    caller commits with the refusal rather than rolling back.
    """

    if coding_contract_version != 1:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease contract is not available"
        )
    allowlist = await active_coding_certification_allowlist(session)
    # Decide the allowlist before any row lock (lock order: allowlist, agent,
    # lease, grant), so a refused caller never waits on or holds the agent row.
    identity = (
        await session.execute(
            select(Agent.sha256, Agent.screened_image_sha256).where(
                Agent.agent_id == agent_id
            )
        )
    ).one_or_none()
    if identity is None:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    artifact_sha256, screened_image_sha256 = identity
    if not allowlist.admits(
        agent_id=agent_id,
        artifact_sha256=artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        validator_hotkey=validator_hotkey,
    ):
        raise CodingCertificationAllowlistRefusedError()
    agent = await session.get(
        Agent, agent_id, with_for_update=True, populate_existing=True
    )
    # A rebuild or re-upload that landed between the unlocked tuple check and
    # the row lock is a different identity; refuse rather than mint for it.
    if (
        agent is None
        or agent.sha256 != artifact_sha256
        or agent.screened_image_sha256 != screened_image_sha256
        or not _screened_image_is_complete(agent)
    ):
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
    now = await database_now(session)
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
    valid_until = await certification_valid_until(
        session,
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        screened_image_sha256=agent.screened_image_sha256,
        bench_version=bench_version,
        coding_contract_version=coding_contract_version,
        now=now,
    )
    if valid_until is not None:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification for this artifact is still valid until "
            f"{valid_until.isoformat()}"
        )
    recent_claims = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingCertificationLease)
            .where(
                CodingCertificationLease.agent_id == agent.agent_id,
                CodingCertificationLease.artifact_sha256 == agent.sha256,
                CodingCertificationLease.screened_image_sha256
                == agent.screened_image_sha256,
                CodingCertificationLease.bench_version == bench_version,
                CodingCertificationLease.coding_contract_version
                == coding_contract_version,
                # Only claims an allowlist revision admitted count, so claims
                # made before the strict allowlist cannot exhaust this budget.
                CodingCertificationLease.claim_allowlist_revision.is_not(None),
                CodingCertificationLease.claimed_at > now - CLAIMED_ATTEMPT_WINDOW,
            )
        )
        or 0
    )
    if recent_claims >= MAX_CLAIMED_ATTEMPTS_PER_IDENTITY:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification attempt budget is exhausted"
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
    """Exclusive claim of an issued lease by the named, allowlisted validator."""

    gate = await lock_coding_certification_lease(
        session, lease_id=lease_id, validator_hotkey=validator_hotkey
    )
    row, now = gate.lease, gate.now
    if await _expire_if_due(session, row, now=now):
        return result_from_row(row, idempotent=False)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        return result_from_row(row, idempotent=True)
    if row.status != CodingCertificationLeaseStatus.ISSUED.value:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    row.status = CodingCertificationLeaseStatus.CLAIMED.value
    row.claimed_at = now
    row.claim_allowlist_revision = gate.allowlist.revision
    await session.flush()
    return result_from_row(row, idempotent=False)


async def abort_coding_certification_lease(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    lease_id: UUID,
) -> CodingCertificationLeaseResult:
    """Abort an unclaimed issued lease.

    A claimed lease cannot be aborted by its validator, so a restart cannot
    create an immediate clean rerun; after its receipt window it only expires.
    """

    row = await session.get(
        CodingCertificationLease, lease_id, with_for_update=True, populate_existing=True
    )
    now = await database_now(session)
    if row is None or row.validator_hotkey != validator_hotkey:
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification lease is not available"
        )
    if row.status == CodingCertificationLeaseStatus.ABORTED.value:
        return result_from_row(row, idempotent=True)
    if await _expire_if_due(session, row, now=now):
        return result_from_row(row, idempotent=False)
    if row.status == CodingCertificationLeaseStatus.CLAIMED.value:
        raise CodingCertificationLeaseConflictError(
            "claimed coding certification lease cannot be aborted"
        )
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


async def list_coding_certification_leases(
    session: AsyncSession,
    *,
    agent_id: UUID | None,
    validator_hotkey: str | None,
    status: CodingCertificationLeaseStatus | None,
    limit: int,
    offset: int,
) -> CodingCertificationLeaseAuditPage:
    """Read-only, newest-first lease audit page. Never transitions a row."""

    filters = []
    if agent_id is not None:
        filters.append(CodingCertificationLease.agent_id == agent_id)
    if validator_hotkey is not None:
        filters.append(CodingCertificationLease.validator_hotkey == validator_hotkey)
    if status is not None:
        filters.append(CodingCertificationLease.status == status.value)
    total = int(
        await session.scalar(
            select(func.count()).select_from(CodingCertificationLease).where(*filters)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(
                CodingCertificationLease,
                CodingCertificationInferenceGrant.status,
                CodingCapabilityCertification.status,
            )
            .outerjoin(
                CodingCertificationInferenceGrant,
                CodingCertificationInferenceGrant.lease_id
                == CodingCertificationLease.lease_id,
            )
            .outerjoin(
                CodingCapabilityCertification,
                CodingCapabilityCertification.lease_id
                == CodingCertificationLease.lease_id,
            )
            .where(*filters)
            .order_by(
                CodingCertificationLease.issued_at.desc(),
                CodingCertificationLease.lease_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return CodingCertificationLeaseAuditPage(
        rows=[
            CodingCertificationLeaseAuditRow(
                lease=lease,
                inference_grant_status=grant_status,
                receipt_status=receipt_status,
            )
            for lease, grant_status, receipt_status in rows
        ],
        total=total,
        now=await database_now(session),
    )


async def authorize_coding_certification_harness_delivery(
    session: AsyncSession,
    *,
    lease_id: UUID,
    validator_hotkey: str,
) -> CodingCertificationHarnessAuthority:
    """Return the current screened image for one claimed certification lease.

    Harness access ends at the lease deadline; the receipt window does not
    extend it.
    """

    gate = await lock_coding_certification_lease(
        session, lease_id=lease_id, validator_hotkey=validator_hotkey
    )
    lease, now = gate.lease, gate.now
    agent = await session.get(Agent, lease.agent_id)
    if (
        agent is None
        or lease.status != CodingCertificationLeaseStatus.CLAIMED.value
        or lease.weight_eligible
        or _aware(lease.deadline) <= now
        or lease.artifact_sha256 != agent.sha256
        or lease.screened_image_sha256 != agent.screened_image_sha256
        or agent.screened_image_sha256 is None
        or agent.screened_image_size_bytes is None
        or agent.screened_image_size_bytes <= 0
        or agent.screened_image_size_bytes > 8 << 30
        or agent.screened_image_id is None
        or agent.screened_image_ref is None
        or agent.screened_image_upload_id is None
        or agent.screening_policy_version < 9
        or agent.screened_image_id != lease.screened_image_id
        or agent.screened_image_ref != lease.screened_image_ref
        or agent.screened_image_upload_id != lease.screened_image_upload_id
        or agent.screened_image_ref != f"ditto-screen/{agent.agent_id}:latest"
    ):
        raise CodingCertificationLeaseNotAvailableError(
            "coding certification harness is unavailable for this validator"
        )
    return CodingCertificationHarnessAuthority(
        agent_id=agent.agent_id,
        lease_id=lease.lease_id,
        deadline=_aware(lease.deadline),
        bench_version=lease.bench_version,
        agent_artifact_sha256=lease.artifact_sha256,
        screened_image_sha256=agent.screened_image_sha256,
        screened_image_size_bytes=agent.screened_image_size_bytes,
        screened_image_id=agent.screened_image_id,
        screened_image_ref=agent.screened_image_ref,
        screened_image_upload_id=agent.screened_image_upload_id,
        screening_policy_version=agent.screening_policy_version,
    )
