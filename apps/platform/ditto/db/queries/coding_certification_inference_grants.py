"""Durable Platform authority for public-canary certification inference grants."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
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
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseNotAvailableError,
    authorize_coding_certification_harness_delivery,
)
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantActivation,
    CodingInferenceGrantConflictError,
    CodingInferenceGrantIntegrityError,
    CodingInferenceGrantNotAvailableError,
    CodingInferenceGrantResult,
    CodingInferenceGrantRevocation,
    coding_inference_bearer_digest,
)

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


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database clock did not return a timestamp")
    return _aware(value)


def _revoke(grant: CodingCertificationInferenceGrant, *, now: datetime) -> None:
    grant.status = "revoked"
    grant.bearer_digest = None
    grant.revoke_bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = now
    grant.updated_at = now


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
    now: datetime,
) -> CodingCertificationLease:
    try:
        await authorize_coding_certification_harness_delivery(
            session, lease_id=lease_id, validator_hotkey=validator_hotkey
        )
    except CodingCertificationLeaseNotAvailableError as error:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference lease is unavailable"
        ) from error
    lease = await session.get(CodingCertificationLease, lease_id, with_for_update=True)
    agent = await session.get(Agent, lease.agent_id) if lease is not None else None
    if (
        lease is None
        or agent is None
        or lease.validator_hotkey != validator_hotkey
        or lease.status != "claimed"
        or lease.weight_eligible
        or _aware(lease.deadline) <= now
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
) -> CodingInferenceGrantResult:
    """Create or return the one immutable policy grant for a claimed lease."""

    try:
        policy = _canonical_policy(policy)
    except ValueError as error:
        raise CodingInferenceGrantIntegrityError(
            "coding certification inference policy is malformed"
        ) from error
    now = await _database_now(session)
    lease = await _claimed_lease_is_live(
        session, lease_id=lease_id, validator_hotkey=validator_hotkey, now=now
    )
    expected = _expected_fields(lease=lease, policy=policy)
    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.lease_id == lease.lease_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is not None:
        if any(getattr(grant, field) != value for field, value in expected.items()):
            _revoke(grant, now=now)
            raise CodingInferenceGrantIntegrityError(
                "stored coding certification inference grant drifted"
            )
        if grant.status in {"revoked", "exhausted"} or _aware(grant.expires_at) <= now:
            if grant.status != "revoked":
                _revoke(grant, now=now)
            raise CodingInferenceGrantNotAvailableError(
                "coding certification inference grant is terminal"
            )
        return CodingInferenceGrantResult(grant=grant, idempotent=True)
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
    return CodingInferenceGrantResult(grant=row, idempotent=False)


async def activate_coding_certification_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    broker_public_key: str,
    policy: CodingInferencePolicy,
) -> CodingInferenceGrantActivation:
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
    now = await _database_now(session)
    lease = await _claimed_lease_is_live(
        session,
        lease_id=snapshot.lease_id,
        validator_hotkey=validator_hotkey,
        now=now,
    )
    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
    ):
        if grant is not None and grant.status not in {"revoked", "exhausted"}:
            _revoke(grant, now=now)
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
    return CodingInferenceGrantActivation(
        grant=grant, bearer=bearer, revoke_bearer=revoke_bearer
    )


async def revoke_coding_certification_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    generation: int,
) -> CodingInferenceGrantRevocation:
    """Durably revoke exactly the caller's observed canary grant generation."""

    grant = await session.scalar(
        select(CodingCertificationInferenceGrant)
        .where(CodingCertificationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await _database_now(session)
    if grant is None or grant.validator_hotkey != validator_hotkey:
        raise CodingInferenceGrantNotAvailableError(
            "coding certification inference grant is unavailable"
        )
    if grant.status == "revoked":
        if grant.generation != generation:
            raise CodingInferenceGrantConflictError(
                "coding certification inference grant generation disagrees"
            )
        return CodingInferenceGrantRevocation(grant=grant, idempotent=True)
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
    _revoke(grant, now=now)
    await session.flush()
    return CodingInferenceGrantRevocation(grant=grant, idempotent=False)


async def revoke_coding_certification_inference_grant_by_capability(
    session: AsyncSession,
    *,
    grant_id: UUID,
    lease_id: UUID,
    generation: int,
    revoke_bearer: str,
) -> CodingInferenceGrantRevocation | None:
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
    now = await _database_now(session)
    if (
        grant is None
        or grant.lease_id != lease_id
        or grant.generation != generation
        or grant.revoke_bearer_digest is None
        or not hmac.compare_digest(grant.revoke_bearer_digest, observed_digest)
    ):
        return None
    if grant.status == "revoked":
        return CodingInferenceGrantRevocation(grant=grant, idempotent=True)
    if grant.status != "active" or grant.active_requests != 0:
        raise CodingInferenceGrantConflictError(
            "coding certification inference grant is not active"
        )
    _revoke(grant, now=now)
    await session.flush()
    return CodingInferenceGrantRevocation(grant=grant, idempotent=False)
