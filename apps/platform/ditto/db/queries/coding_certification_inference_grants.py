"""Durable Platform authority for public-canary certification inference grants."""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import not_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    effective_inference_request_budget,
    policy_digest,
)
from ditto.db.models import (
    Agent,
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_allowlist import (
    CodingCertificationAllowlist,
    CodingCertificationAllowlistRefusedError,
)
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseNotAvailableError,
    authorize_coding_certification_harness_delivery,
    database_now,
    mark_certification_inference_grant_revoked,
    revoke_live_certification_inference_grants,
)
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantConflictError,
    CodingInferenceGrantIntegrityError,
    CodingInferenceGrantNotAvailableError,
    coding_inference_bearer_digest,
)


@dataclass(frozen=True)
class CodingCertificationInferenceGrantResult:
    grant: CodingCertificationInferenceGrant
    idempotent: bool


@dataclass(frozen=True)
class CodingCertificationInferenceGrantRevocation:
    grant: CodingCertificationInferenceGrant
    idempotent: bool


@dataclass(frozen=True)
class CodingCertificationInferenceGrantActivation:
    grant: CodingCertificationInferenceGrant
    bearer: str
    revoke_bearer: str


_CANARY_CASE_ID = "PRACTICE-LEDGER-001"
_CANARY_PROFILE_ID = "public-certification-v1"
_PROMPT_BUDGET = 10_000
_COMPLETION_BUDGET = 2_000
_TOOL_CALLS = 16


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _canonical_policy(policy: CodingInferencePolicy) -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )


def _expected_fields(
    *,
    lease: CodingCertificationLease,
    policy: CodingInferencePolicy,
) -> dict[str, object]:
    return {
        "lease_id": lease.lease_id,
        "validator_hotkey": lease.validator_hotkey,
        "case_id": _CANARY_CASE_ID,
        "profile_capability_id": _CANARY_PROFILE_ID,
        "inference_grant_sha256": policy_digest(policy),
        "model": policy.model,
        "provider_api": policy.provider_api,
        "provider_route": policy.provider_route,
        "receipt_provider": policy.receipt_provider,
        "provider_route_profile": policy.provider_route_profile,
        "provider_account_guardrail": policy.provider_account_guardrail,
        "provider_pipeline_policy": policy.provider_pipeline_policy,
        "provider_cache_policy": policy.provider_cache_policy,
        "reasoning_effort": policy.reasoning_effort,
        "request_budget": effective_inference_request_budget(_TOOL_CALLS),
        "prompt_token_budget": min(_PROMPT_BUDGET, policy.max_prompt_tokens),
        "completion_token_budget": min(
            _COMPLETION_BUDGET, policy.max_completion_tokens
        ),
        "cost_budget_usd_micros": policy.max_cost_usd_micros,
        "expires_at": _aware(lease.deadline),
        "weight_eligible": False,
    }


async def _claimed_lease_is_live(
    session: AsyncSession,
    *,
    lease_id: UUID,
    validator_hotkey: str,
) -> CodingCertificationLease:
    """Pass the shared lease gate (allowlist, claimed, deadline, image) or refuse.

    An allowlist refusal also terminally revokes the lease's live grant, and
    that revocation commits with the refusal.
    """

    try:
        await authorize_coding_certification_harness_delivery(
            session, lease_id=lease_id, validator_hotkey=validator_hotkey
        )
    except CodingCertificationAllowlistRefusedError:
        await revoke_live_certification_inference_grants(
            session, lease_id=lease_id, now=await database_now(session)
        )
        await session.flush()
        raise
    except CodingCertificationLeaseNotAvailableError as error:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference lease is unavailable"
        ) from error
    lease = await session.get(CodingCertificationLease, lease_id)
    agent = await session.get(Agent, lease.agent_id) if lease is not None else None
    if (
        lease is None
        or agent is None
        or lease.validator_hotkey != validator_hotkey
        or lease.status != "claimed"
        or lease.weight_eligible
        or lease.artifact_sha256 != agent.sha256
        or lease.screened_image_sha256 != agent.screened_image_sha256
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference lease is unavailable"
        )
    return lease


async def ensure_coding_certification_inference_grant(
    session: AsyncSession,
    *,
    lease_id: UUID,
    validator_hotkey: str,
    policy: CodingInferencePolicy,
) -> CodingCertificationInferenceGrantResult:
    """Create or return the one immutable policy grant for a claimed lease."""

    try:
        policy = _canonical_policy(policy)
    except ValueError as error:
        raise CodingInferenceGrantIntegrityError(
            "coding certification inference policy is malformed"
        ) from error
    lease = await _claimed_lease_is_live(
        session, lease_id=lease_id, validator_hotkey=validator_hotkey
    )
    expected = _expected_fields(lease=lease, policy=policy)
    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.lease_id == lease.lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await database_now(session)
    if grant is not None:
        if any(getattr(grant, field) != value for field, value in expected.items()):
            mark_certification_inference_grant_revoked(grant, now=now)
            raise CodingInferenceGrantIntegrityError(
                "stored coding certification inference grant drifted"
            )
        if grant.status in {"revoked", "exhausted"} or _aware(grant.expires_at) <= now:
            if grant.status != "revoked":
                mark_certification_inference_grant_revoked(grant, now=now)
            raise CodingInferenceGrantNotAvailableError(
                "coding certification inference grant is terminal"
            )
        return CodingCertificationInferenceGrantResult(grant=grant, idempotent=True)
    if _aware(lease.deadline) <= now:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference lease is unavailable"
        )
    row = CodingCertificationInferenceGrant(
        grant_id=uuid4(),
        **expected,
        status="pending",
        bearer_digest=None,
        revoke_bearer_digest=None,
        broker_public_key=None,
        generation=0,
        request_count=0,
        prompt_tokens=0,
        completion_tokens=0,
        cost_usd_micros=0,
        active_requests=0,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return CodingCertificationInferenceGrantResult(grant=row, idempotent=False)


async def activate_coding_certification_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    broker_public_key: str,
    policy: CodingInferencePolicy,
) -> CodingCertificationInferenceGrantActivation:
    """Rotate a live canary grant onto one broker key."""

    try:
        policy = _canonical_policy(policy)
    except ValueError as error:
        raise CodingInferenceGrantIntegrityError(
            "coding certification inference policy is malformed"
        ) from error
    normalized_broker_key = broker_public_key.rstrip("=")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", normalized_broker_key) is None:
        raise CodingInferenceGrantIntegrityError(
            "coding certification inference broker key is malformed"
        )
    snapshot = await session.get(CodingCertificationInferenceGrant, grant_id)
    if snapshot is None or snapshot.validator_hotkey != validator_hotkey:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference grant is unavailable"
        )
    lease = await _claimed_lease_is_live(
        session,
        lease_id=snapshot.lease_id,
        validator_hotkey=validator_hotkey,
    )
    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await database_now(session)
    expected = _expected_fields(lease=lease, policy=policy)
    if (
        grant is None
        or grant.validator_hotkey != validator_hotkey
        or grant.lease_id != lease.lease_id
        or any(getattr(grant, field) != value for field, value in expected.items())
        or grant.status in {"revoked", "exhausted"}
        or grant.active_requests != 0
        or grant.generation >= (1 << 31) - 1
        or _aware(grant.expires_at) <= now
        or _aware(lease.deadline) <= now
    ):
        if grant is not None and grant.status not in {"revoked", "exhausted"}:
            mark_certification_inference_grant_revoked(grant, now=now)
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference grant is not live"
        )
    bearer = secrets.token_urlsafe(32)
    revoke_bearer = secrets.token_urlsafe(32)
    while revoke_bearer == bearer:
        revoke_bearer = secrets.token_urlsafe(32)
    grant.bearer_digest = coding_inference_bearer_digest(bearer)
    grant.revoke_bearer_digest = coding_inference_bearer_digest(revoke_bearer)
    grant.broker_public_key = normalized_broker_key
    grant.generation += 1
    grant.status = "active"
    grant.revoked_at = None
    grant.updated_at = now
    await session.flush()
    return CodingCertificationInferenceGrantActivation(
        grant=grant, bearer=bearer, revoke_bearer=revoke_bearer
    )


async def revoke_coding_certification_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    generation: int,
) -> CodingCertificationInferenceGrantRevocation:
    """Durably revoke exactly the caller's observed canary grant generation."""

    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await database_now(session)
    if grant is None or grant.validator_hotkey != validator_hotkey:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference grant is unavailable"
        )
    if grant.status == "revoked":
        if grant.generation != generation:
            raise CodingInferenceGrantConflictError(
                "coding certification inference grant generation disagrees"
            )
        return CodingCertificationInferenceGrantRevocation(grant=grant, idempotent=True)
    if grant.generation != generation:
        raise CodingInferenceGrantConflictError(
            "coding certification inference grant generation disagrees"
        )
    if grant.status != "active" and grant.status != "pending":
        raise CodingInferenceGrantConflictError(
            "coding certification inference grant is not revocable"
        )
    if grant.active_requests != 0:
        raise CodingInferenceGrantConflictError(
            "coding certification inference grant still has an active request"
        )
    mark_certification_inference_grant_revoked(grant, now=now)
    await session.flush()
    return CodingCertificationInferenceGrantRevocation(grant=grant, idempotent=False)


async def revoke_coding_certification_inference_grant_by_capability(
    session: AsyncSession,
    *,
    grant_id: UUID,
    lease_id: UUID,
    generation: int,
    revoke_bearer: str,
) -> CodingCertificationInferenceGrantRevocation | None:
    """Idempotently revoke one active canary generation through its bearer."""

    if (
        generation < 1
        or generation > (1 << 31) - 1
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", revoke_bearer) is None
    ):
        return None
    observed_digest = coding_inference_bearer_digest(revoke_bearer)
    snapshot = await session.get(CodingCertificationInferenceGrant, grant_id)
    if (
        snapshot is None
        or snapshot.lease_id != lease_id
        or snapshot.generation != generation
        or snapshot.revoke_bearer_digest is None
        or not hmac.compare_digest(snapshot.revoke_bearer_digest, observed_digest)
    ):
        return None
    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await database_now(session)
    if (
        grant is None
        or grant.lease_id != lease_id
        or grant.generation != generation
        or grant.revoke_bearer_digest is None
        or not hmac.compare_digest(grant.revoke_bearer_digest, observed_digest)
    ):
        return None
    if grant.status == "revoked":
        return CodingCertificationInferenceGrantRevocation(grant=grant, idempotent=True)
    if grant.status != "active" or grant.active_requests != 0:
        raise CodingInferenceGrantConflictError(
            "coding certification inference grant is not active"
        )
    mark_certification_inference_grant_revoked(grant, now=now)
    await session.flush()
    return CodingCertificationInferenceGrantRevocation(grant=grant, idempotent=False)


async def revoke_unlisted_coding_certification_inference_grants(
    session: AsyncSession,
    *,
    allowlist: CodingCertificationAllowlist,
) -> int:
    """Terminally revoke every live canary grant the given allowlist refuses.

    Called in the same transaction that appends an allowlist revision, after
    the exclusive allowlist lock is held and in-flight leases were aborted, so
    tightening the restriction also cuts off Platform-paid inference already
    minted for unlisted tuples. The tuple filter runs in SQL, so only refused
    grants are locked.
    """

    statement = (
        select(CodingCertificationInferenceGrant)
        .join(
            CodingCertificationLease,
            CodingCertificationLease.lease_id
            == CodingCertificationInferenceGrant.lease_id,
        )
        .where(CodingCertificationInferenceGrant.status.in_(("pending", "active")))
    )
    if allowlist.tuples:
        statement = statement.where(
            not_(
                tuple_(
                    CodingCertificationLease.agent_id,
                    CodingCertificationLease.artifact_sha256,
                    CodingCertificationLease.screened_image_sha256,
                    CodingCertificationInferenceGrant.validator_hotkey,
                ).in_(
                    [
                        (
                            UUID(agent_id),
                            artifact_sha256,
                            screened_image_sha256,
                            validator_hotkey,
                        )
                        for (
                            agent_id,
                            artifact_sha256,
                            screened_image_sha256,
                            validator_hotkey,
                        ) in sorted(allowlist.tuples)
                    ]
                )
            )
        )
    grants = (
        await session.scalars(
            statement.order_by(CodingCertificationInferenceGrant.grant_id)
            .with_for_update(of=CodingCertificationInferenceGrant)
            .execution_options(populate_existing=True)
        )
    ).all()
    if not grants:
        return 0
    now = await database_now(session)
    for grant in grants:
        mark_certification_inference_grant_revoked(grant, now=now)
    await session.flush()
    return len(grants)
