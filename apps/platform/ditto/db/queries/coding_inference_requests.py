"""Durable request and provider-settlement ledger for shadow coding Luna."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    CodingInferenceProviderSettlement,
    CodingInferenceReceiptOutcome,
    policy_digest,
    provider_settlement_digest,
)
from ditto.db.models import CodingInferenceGrant, CodingInferenceRequest
from ditto.db.queries.coding_inference_grants import (
    coding_inference_bearer_digest,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_MAX_SETTLEMENT_JSON_BYTES = 65_536
_UNSETTLED_REASONS = frozenset(
    {
        "provider_settlement_unavailable",
        "provider_response_lost",
        "relay_infrastructure",
        "invalid_provider_settlement",
    }
)


class CodingInferenceRequestNotAvailableError(Exception):
    """The grant or request is not available to the caller."""


class CodingInferenceRequestIntegrityError(Exception):
    """Trusted request, grant, policy, or settlement identity disagrees."""


class CodingInferenceRequestConflictError(Exception):
    """The requested request-ledger transition conflicts with durable state."""


@dataclass(frozen=True)
class CodingInferenceDispatchAuthority:
    """Trusted identity fixed before a provider request may be dispatched."""

    grant_id: UUID
    ticket_id: UUID
    generation: int
    sequence: int
    request_sequence: int
    attempt: int
    request_id: UUID
    case_id: str
    profile_capability_id: str
    inference_grant_sha256: str
    locked_request_sha256: str


@dataclass(frozen=True)
class CodingInferenceRequestResult:
    request: CodingInferenceRequest
    idempotent: bool


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    return _aware(value)


def _canonical_policy(policy: CodingInferencePolicy) -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )


def _canonical_settlement(
    settlement: CodingInferenceProviderSettlement,
) -> CodingInferenceProviderSettlement:
    return CodingInferenceProviderSettlement.model_validate_json(
        settlement.model_dump_json(by_alias=True)
    )


def _valid_identifier(value: object) -> bool:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        return False
    try:
        return len(value.encode("utf-8")) <= 256
    except UnicodeEncodeError:
        return False


def _validate_authority(authority: CodingInferenceDispatchAuthority) -> None:
    if (
        not isinstance(authority, CodingInferenceDispatchAuthority)
        or not isinstance(authority.grant_id, UUID)
        or not isinstance(authority.ticket_id, UUID)
        or not isinstance(authority.request_id, UUID)
        or type(authority.generation) is not int
        or type(authority.sequence) is not int
        or type(authority.request_sequence) is not int
        or type(authority.attempt) is not int
        or not isinstance(authority.inference_grant_sha256, str)
        or not isinstance(authority.locked_request_sha256, str)
        or authority.grant_id.int == 0
        or authority.ticket_id.int == 0
        or authority.request_id.int == 0
        or not 1 <= authority.generation <= (1 << 31) - 1
        or not 1 <= authority.sequence <= 1100
        or not 1 <= authority.request_sequence <= 256
        or not 1 <= authority.attempt <= 3
        or not _valid_identifier(authority.case_id)
        or not _valid_identifier(authority.profile_capability_id)
        or _SHA256_RE.fullmatch(authority.inference_grant_sha256) is None
        or _SHA256_RE.fullmatch(authority.locked_request_sha256) is None
    ):
        raise CodingInferenceRequestIntegrityError(
            "coding inference dispatch authority is malformed"
        )


def _authority_matches_grant(
    authority: CodingInferenceDispatchAuthority,
    grant: CodingInferenceGrant,
) -> bool:
    return bool(
        authority.grant_id == grant.grant_id
        and authority.ticket_id == grant.ticket_id
        and authority.generation == grant.generation
        and authority.case_id == grant.case_id
        and authority.profile_capability_id == grant.profile_capability_id
        and authority.inference_grant_sha256 == grant.inference_grant_sha256
        and grant.weight_eligible is False
    )


def _authority_matches_grant_identity(
    authority: CodingInferenceDispatchAuthority,
    grant: CodingInferenceGrant,
) -> bool:
    return bool(
        authority.grant_id == grant.grant_id
        and authority.ticket_id == grant.ticket_id
        and authority.case_id == grant.case_id
        and authority.profile_capability_id == grant.profile_capability_id
        and authority.inference_grant_sha256 == grant.inference_grant_sha256
        and grant.weight_eligible is False
    )


def _authority_matches_request(
    authority: CodingInferenceDispatchAuthority,
    request: CodingInferenceRequest,
) -> bool:
    return all(
        (
            authority.grant_id == request.grant_id,
            authority.ticket_id == request.ticket_id,
            authority.generation == request.generation,
            authority.sequence == request.sequence,
            authority.request_sequence == request.request_sequence,
            authority.attempt == request.attempt,
            authority.request_id == request.request_id,
            authority.case_id == request.case_id,
            authority.profile_capability_id == request.profile_capability_id,
            authority.inference_grant_sha256 == request.inference_grant_sha256,
            authority.locked_request_sha256 == request.locked_request_sha256,
            request.weight_eligible is False,
        )
    )


def _grant_matches_policy(
    grant: CodingInferenceGrant,
    policy: CodingInferencePolicy,
) -> bool:
    return bool(
        grant.inference_grant_sha256 == policy_digest(policy)
        and grant.model == policy.model
        and grant.provider_api == policy.provider_api
        and grant.provider_route == policy.provider_route
        and grant.receipt_provider == policy.receipt_provider
        and grant.provider_route_profile == policy.provider_route_profile
        and grant.provider_account_guardrail == policy.provider_account_guardrail
        and grant.provider_pipeline_policy == policy.provider_pipeline_policy
        and grant.provider_cache_policy == policy.provider_cache_policy
        and grant.reasoning_effort == policy.reasoning_effort
        and grant.request_budget <= policy.max_requests
        and grant.prompt_token_budget <= policy.max_prompt_tokens
        and grant.completion_token_budget <= policy.max_completion_tokens
        and grant.cost_budget_usd_micros <= policy.max_cost_usd_micros
        and grant.weight_eligible is False
    )


def _revoke_grant(grant: CodingInferenceGrant, now: datetime) -> None:
    grant.status = "revoked"
    grant.bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = now
    grant.updated_at = now


def _exhaust_grant(grant: CodingInferenceGrant, now: datetime) -> None:
    grant.status = "exhausted"
    grant.bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = None
    grant.updated_at = now


def _grant_usage_budget_exhausted(grant: CodingInferenceGrant) -> bool:
    return bool(
        grant.prompt_tokens >= grant.prompt_token_budget
        or grant.completion_tokens >= grant.completion_token_budget
        or grant.cost_usd_micros >= grant.cost_budget_usd_micros
    )


def _grant_budget_exhausted(grant: CodingInferenceGrant) -> bool:
    return bool(
        grant.request_count >= grant.request_budget
        or _grant_usage_budget_exhausted(grant)
    )


async def _locked_grant(
    session: AsyncSession,
    grant_id: UUID,
) -> CodingInferenceGrant | None:
    return await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _latest_request(
    session: AsyncSession,
    grant_id: UUID,
) -> CodingInferenceRequest | None:
    return await session.scalar(
        select(CodingInferenceRequest)
        .where(CodingInferenceRequest.grant_id == grant_id)
        .order_by(CodingInferenceRequest.sequence.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def begin_coding_inference_request(
    session: AsyncSession,
    *,
    authority: CodingInferenceDispatchAuthority,
    bearer: str,
) -> CodingInferenceRequestResult:
    """Atomically reserve one ordered provider dispatch under a live grant."""

    _validate_authority(authority)
    if (
        not isinstance(bearer, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", bearer) is None
    ):
        raise CodingInferenceRequestNotAvailableError(
            "coding inference grant is unavailable"
        )
    grant = await _locked_grant(session, authority.grant_id)
    latest = await _latest_request(session, authority.grant_id)
    now = await _database_now(session)
    if (
        grant is None
        or not grant.bearer_digest
        or not secrets.compare_digest(
            grant.bearer_digest,
            coding_inference_bearer_digest(bearer),
        )
    ):
        raise CodingInferenceRequestNotAvailableError(
            "coding inference grant is unavailable"
        )
    if (
        not _authority_matches_grant(authority, grant)
        or grant.status != "active"
        or _aware(grant.expires_at) <= now
    ):
        if grant.status == "active" and _aware(grant.expires_at) <= now:
            _revoke_grant(grant, now)
        raise CodingInferenceRequestNotAvailableError(
            "coding inference grant is not live"
        )
    if latest is not None and latest.sequence == authority.sequence:
        if not _authority_matches_request(authority, latest):
            raise CodingInferenceRequestConflictError(
                "coding inference request replay drifted"
            )
        if latest.status != "started":
            raise CodingInferenceRequestConflictError(
                "coding inference request is already terminal"
            )
        if grant.active_requests != 1:
            raise CodingInferenceRequestIntegrityError(
                "coding inference active-request accounting drifted"
            )
        return CodingInferenceRequestResult(request=latest, idempotent=True)
    if grant.active_requests != 0:
        raise CodingInferenceRequestConflictError(
            "coding inference grant already has an active request"
        )
    if (latest is None and grant.request_count != 0) or (
        latest is not None and grant.request_count != latest.request_sequence
    ):
        raise CodingInferenceRequestIntegrityError(
            "coding inference request history accounting drifted"
        )

    if latest is None:
        expected = (1, 1, 1)
        increments_request_count = True
    elif latest.status == "receipt_free_retry":
        expected = (
            latest.sequence + 1,
            latest.request_sequence,
            latest.attempt + 1,
        )
        increments_request_count = False
        if (
            authority.request_id != latest.request_id
            or authority.locked_request_sha256 != latest.locked_request_sha256
        ):
            raise CodingInferenceRequestConflictError(
                "coding inference retry identity drifted"
            )
    elif latest.status == "complete":
        expected = (latest.sequence + 1, latest.request_sequence + 1, 1)
        increments_request_count = True
    else:
        raise CodingInferenceRequestConflictError(
            "coding inference request history is terminal"
        )
    if (
        authority.sequence,
        authority.request_sequence,
        authority.attempt,
    ) != expected:
        raise CodingInferenceRequestConflictError(
            "coding inference request order drifted"
        )
    if _grant_usage_budget_exhausted(grant) or (
        increments_request_count and grant.request_count >= grant.request_budget
    ):
        _exhaust_grant(grant, now)
        raise CodingInferenceRequestNotAvailableError(
            "coding inference grant budget is exhausted"
        )

    request = CodingInferenceRequest(
        request_row_id=uuid4(),
        grant_id=authority.grant_id,
        ticket_id=authority.ticket_id,
        generation=authority.generation,
        sequence=authority.sequence,
        request_sequence=authority.request_sequence,
        attempt=authority.attempt,
        request_id=authority.request_id,
        case_id=authority.case_id,
        profile_capability_id=authority.profile_capability_id,
        inference_grant_sha256=authority.inference_grant_sha256,
        locked_request_sha256=authority.locked_request_sha256,
        status="started",
        provider_settlement_sha256=None,
        provider_generation_id=None,
        provider_settlement_json=None,
        unsettled_reason=None,
        started_at=now,
        settled_at=None,
        weight_eligible=False,
    )
    session.add(request)
    if increments_request_count:
        grant.request_count += 1
    grant.active_requests = 1
    grant.updated_at = now
    await session.flush()
    return CodingInferenceRequestResult(request=request, idempotent=False)


def _settlement_matches_authority(
    settlement: CodingInferenceProviderSettlement,
    authority: CodingInferenceDispatchAuthority,
) -> bool:
    return bool(
        settlement.ticket_id == authority.ticket_id
        and settlement.case_id == authority.case_id
        and settlement.profile_capability_id == authority.profile_capability_id
        and settlement.inference_grant_sha256 == authority.inference_grant_sha256
        and settlement.grant_id == authority.grant_id
        and settlement.generation == authority.generation
        and settlement.request_id == authority.request_id
        and settlement.request_sequence == authority.request_sequence
        and settlement.attempt == authority.attempt
        and settlement.locked_request_sha256 == authority.locked_request_sha256
    )


async def settle_coding_inference_request(
    session: AsyncSession,
    *,
    authority: CodingInferenceDispatchAuthority,
    settlement: CodingInferenceProviderSettlement,
    policy: CodingInferencePolicy,
) -> CodingInferenceRequestResult:
    """Persist one trusted settlement and account its real provider spend."""

    _validate_authority(authority)
    try:
        policy = _canonical_policy(policy)
        settlement = _canonical_settlement(settlement)
        settlement_sha256 = provider_settlement_digest(settlement, policy)
    except ValueError as error:
        raise CodingInferenceRequestIntegrityError(
            "coding inference settlement is malformed"
        ) from error
    if not _settlement_matches_authority(settlement, authority):
        raise CodingInferenceRequestIntegrityError(
            "coding inference settlement authority drifted"
        )
    settlement_json = settlement.model_dump_json(by_alias=True)
    if len(settlement_json.encode()) > _MAX_SETTLEMENT_JSON_BYTES:
        raise CodingInferenceRequestIntegrityError(
            "coding inference settlement exceeds its durable bound"
        )

    grant = await _locked_grant(session, authority.grant_id)
    request = await session.scalar(
        select(CodingInferenceRequest)
        .where(
            CodingInferenceRequest.grant_id == authority.grant_id,
            CodingInferenceRequest.sequence == authority.sequence,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await _database_now(session)
    if grant is None or request is None:
        raise CodingInferenceRequestNotAvailableError(
            "coding inference request is unavailable"
        )
    if (
        not _authority_matches_request(authority, request)
        or not _authority_matches_grant_identity(authority, grant)
        or not _grant_matches_policy(grant, policy)
        or grant.request_count < request.request_sequence
    ):
        raise CodingInferenceRequestIntegrityError(
            "coding inference request authority drifted"
        )
    if request.status != "started":
        if request.status == settlement.outcome.value:
            try:
                stored = CodingInferenceProviderSettlement.model_validate_json(
                    request.provider_settlement_json or ""
                )
                stored_sha256 = provider_settlement_digest(stored, policy)
            except ValueError as error:
                raise CodingInferenceRequestIntegrityError(
                    "stored coding inference settlement is corrupt"
                ) from error
            if (
                request.provider_settlement_sha256 == settlement_sha256
                and stored_sha256 == settlement_sha256
                and stored == settlement
                and request.provider_generation_id == settlement.provider_generation_id
            ):
                return CodingInferenceRequestResult(request=request, idempotent=True)
        raise CodingInferenceRequestConflictError(
            "coding inference settlement conflicts with durable state"
        )
    if not _authority_matches_grant(authority, grant):
        raise CodingInferenceRequestIntegrityError(
            "coding inference grant generation drifted"
        )
    if grant.request_count != request.request_sequence:
        raise CodingInferenceRequestIntegrityError(
            "coding inference active request accounting drifted"
        )
    if grant.status not in {"active", "revoked"} or (
        grant.status == "active" and grant.active_requests != 1
    ):
        raise CodingInferenceRequestIntegrityError(
            "coding inference grant cannot settle this request"
        )
    duplicate_conditions = [
        CodingInferenceRequest.provider_settlement_sha256 == settlement_sha256
    ]
    if settlement.provider_generation_id is not None:
        duplicate_conditions.append(
            CodingInferenceRequest.provider_generation_id
            == settlement.provider_generation_id
        )
    duplicate = await session.scalar(
        select(CodingInferenceRequest.request_row_id)
        .where(
            CodingInferenceRequest.request_row_id != request.request_row_id,
            or_(*duplicate_conditions),
        )
        .limit(1)
    )
    if duplicate is not None:
        raise CodingInferenceRequestIntegrityError(
            "coding inference provider settlement identity was reused"
        )

    request.status = settlement.outcome.value
    request.provider_settlement_sha256 = settlement_sha256
    request.provider_generation_id = settlement.provider_generation_id
    request.provider_settlement_json = settlement_json
    request.unsettled_reason = None
    request.settled_at = now
    grant.prompt_tokens += settlement.prompt_tokens
    grant.completion_tokens += settlement.completion_tokens
    grant.cost_usd_micros += settlement.cost_usd_micros
    grant.active_requests = 0
    grant.updated_at = now

    if grant.status == "active":
        if settlement.outcome is CodingInferenceReceiptOutcome.PROVIDER_FAILURE or (
            settlement.outcome is CodingInferenceReceiptOutcome.RECEIPT_FREE_RETRY
            and settlement.attempt >= policy.max_attempts_per_request
        ):
            _revoke_grant(grant, now)
        elif (
            settlement.outcome is CodingInferenceReceiptOutcome.COMPLETE
            and _grant_budget_exhausted(grant)
        ) or _grant_usage_budget_exhausted(grant):
            _exhaust_grant(grant, now)
    await session.flush()
    return CodingInferenceRequestResult(request=request, idempotent=False)


async def fail_coding_inference_request_unsettled(
    session: AsyncSession,
    *,
    authority: CodingInferenceDispatchAuthority,
    reason: str,
) -> CodingInferenceRequestResult:
    """Terminally revoke a request whose provider settlement is unavailable."""

    _validate_authority(authority)
    if not isinstance(reason, str) or reason not in _UNSETTLED_REASONS:
        raise CodingInferenceRequestIntegrityError(
            "coding inference unsettled reason is invalid"
        )
    grant = await _locked_grant(session, authority.grant_id)
    request = await session.scalar(
        select(CodingInferenceRequest)
        .where(
            CodingInferenceRequest.grant_id == authority.grant_id,
            CodingInferenceRequest.sequence == authority.sequence,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await _database_now(session)
    if grant is None or request is None:
        raise CodingInferenceRequestNotAvailableError(
            "coding inference request is unavailable"
        )
    if not _authority_matches_grant(authority, grant) or not _authority_matches_request(
        authority, request
    ):
        raise CodingInferenceRequestIntegrityError(
            "coding inference request authority drifted"
        )
    if request.status != "started":
        if request.status == "unsettled" and request.unsettled_reason == reason:
            return CodingInferenceRequestResult(request=request, idempotent=True)
        raise CodingInferenceRequestConflictError(
            "coding inference unsettled failure conflicts with durable state"
        )
    request.status = "unsettled"
    request.provider_settlement_sha256 = None
    request.provider_generation_id = None
    request.provider_settlement_json = None
    request.unsettled_reason = reason
    request.settled_at = now
    if grant.status == "active":
        _revoke_grant(grant, now)
    else:
        grant.active_requests = 0
        grant.updated_at = now
    await session.flush()
    return CodingInferenceRequestResult(request=request, idempotent=False)


__all__ = [
    "CodingInferenceDispatchAuthority",
    "CodingInferenceRequestConflictError",
    "CodingInferenceRequestIntegrityError",
    "CodingInferenceRequestNotAvailableError",
    "CodingInferenceRequestResult",
    "begin_coding_inference_request",
    "fail_coding_inference_request_unsettled",
    "settle_coding_inference_request",
]
