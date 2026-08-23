"""Durable Platform authority for shadow coding Luna grants."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    effective_inference_request_budget,
    policy_digest,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingSelectionRunManifest,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingInferenceGrant,
    CodingShadowAuthoringFreeze,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries.coding_certifications import coding_certification_stale_reason
from ditto.db.queries.coding_task_leases import CodingShadowTaskLeaseCore


class CodingInferenceGrantNotAvailableError(Exception):
    """The ticket, grant, certification, or authoring phase is unavailable."""


class CodingInferenceGrantIntegrityError(Exception):
    """Stored authority or a reconstructed lease disagrees."""


class CodingInferenceGrantConflictError(Exception):
    """The requested grant generation cannot make this transition."""


@dataclass(frozen=True)
class CodingInferenceGrantResult:
    grant: CodingInferenceGrant
    idempotent: bool


@dataclass(frozen=True)
class CodingInferenceGrantRevocation:
    grant: CodingInferenceGrant
    idempotent: bool


@dataclass(frozen=True)
class CodingInferenceGrantActivation:
    grant: CodingInferenceGrant
    bearer: str
    revoke_bearer: str


def coding_inference_bearer_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _canonical_policy(policy: CodingInferencePolicy) -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate_json(
        policy.model_dump_json(by_alias=True)
    )


def _canonical_manifest(
    manifest: CodingSelectionRunManifest,
) -> CodingSelectionRunManifest:
    return CodingSelectionRunManifest.model_validate_json(
        manifest.model_dump_json(by_alias=True)
    )


def _canonical_budgets(budgets: CodingCatalogBudgets) -> CodingCatalogBudgets:
    return CodingCatalogBudgets.model_validate_json(budgets.model_dump_json())


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    return _aware(value)


async def _authoring_is_open(
    session: AsyncSession,
    *,
    ticket: CodingShadowTicket,
    run: CodingShadowRun,
    now: datetime,
) -> bool:
    certification = await session.get(
        CodingCapabilityCertification,
        ticket.certification_row_id,
    )
    agent = await session.get(Agent, run.agent_id)
    freeze = await session.scalar(
        select(CodingShadowAuthoringFreeze.freeze_id).where(
            CodingShadowAuthoringFreeze.ticket_id == ticket.ticket_id
        )
    )
    result = await session.scalar(
        select(CodingShadowResult.result_id).where(
            CodingShadowResult.ticket_id == ticket.ticket_id
        )
    )
    return bool(
        certification is not None
        and agent is not None
        and freeze is None
        and result is None
        and ticket.run_row_id == run.run_row_id
        and ticket.task_count == run.task_count == 1
        and ticket.validator_hotkey == certification.validator_hotkey
        and certification.agent_id == run.agent_id
        and certification.artifact_sha256 == run.artifact_sha256
        and certification.screened_image_sha256 == run.screened_image_sha256
        and certification.bench_version == run.bench_version
        and certification.coding_contract_version == run.coding_contract_version
        and _aware(ticket.deadline) > now
        and _aware(certification.expires_at) > _aware(ticket.deadline)
        and coding_certification_stale_reason(certification, agent, now=now) == "active"
        and run.coding_contract_version == 1
        and run.weight_eligible is False
    )


def _expected_fields(
    *,
    ticket: CodingShadowTicket,
    run: CodingShadowRun,
    manifest: CodingSelectionRunManifest,
    budgets: CodingCatalogBudgets,
    policy: CodingInferencePolicy,
) -> dict[str, object]:
    selected = manifest.tasks[0]
    return {
        "ticket_id": ticket.ticket_id,
        "run_row_id": run.run_row_id,
        "task_count": 1,
        "validator_hotkey": ticket.validator_hotkey,
        "case_id": selected.case_id,
        "profile_capability_id": selected.profile_capability_id,
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
        "request_budget": effective_inference_request_budget(
            budgets.workspace_tool_calls
        ),
        "prompt_token_budget": min(
            budgets.model_input_tokens,
            policy.max_prompt_tokens,
        ),
        "completion_token_budget": min(
            budgets.model_output_tokens,
            policy.max_completion_tokens,
        ),
        "cost_budget_usd_micros": policy.max_cost_usd_micros,
        "expires_at": _aware(ticket.deadline),
        "weight_eligible": False,
    }


def _grant_matches(grant: CodingInferenceGrant, expected: dict[str, object]) -> bool:
    return all(getattr(grant, field) == value for field, value in expected.items())


def _revoke(grant: CodingInferenceGrant, *, now: datetime) -> None:
    grant.status = "revoked"
    grant.bearer_digest = None
    grant.broker_public_key = None
    grant.active_requests = 0
    grant.revoked_at = now
    grant.updated_at = now


async def ensure_coding_inference_grant(
    session: AsyncSession,
    *,
    lease: CodingShadowTaskLeaseCore,
    policy: CodingInferencePolicy,
) -> CodingInferenceGrantResult:
    """Create or return the one immutable policy grant for a private task lease."""

    try:
        policy = _canonical_policy(policy)
        manifest = _canonical_manifest(lease.run_manifest)
        budgets = _canonical_budgets(lease.budgets)
    except ValueError as error:
        raise CodingInferenceGrantIntegrityError(
            "coding inference grant input is malformed"
        ) from error
    if (
        lease.weight_eligible is not False
        or len(manifest.tasks) != 1
        or manifest.weight_eligible is not False
        or manifest.coding_contract_version != 1
    ):
        raise CodingInferenceGrantIntegrityError(
            "coding inference grant requires one shadow task"
        )

    ticket = await session.get(
        CodingShadowTicket, lease.ticket_id, with_for_update=True
    )
    run = (
        await session.get(CodingShadowRun, ticket.run_row_id)
        if ticket is not None
        else None
    )
    now = await _database_now(session)
    if (
        ticket is None
        or run is None
        or ticket.validator_hotkey != lease.validator_hotkey
        or ticket.run_row_id != lease.run_row_id
        or _aware(ticket.issued_at) != _aware(lease.issued_at)
        or _aware(ticket.deadline) != _aware(lease.deadline)
        or not await _authoring_is_open(session, ticket=ticket, run=run, now=now)
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding inference ticket is not available"
        )
    grant_sha256 = policy_digest(policy)
    if (
        run.inference_grant_sha256 != grant_sha256
        or manifest.inference_grant_sha256 != grant_sha256
        or manifest.coding_run_id != run.coding_run_id
        or manifest.agent_id != str(run.agent_id)
        or manifest.agent_artifact_sha256 != run.artifact_sha256
    ):
        raise CodingInferenceGrantIntegrityError(
            "coding run does not bind the locked inference policy"
        )
    expected = _expected_fields(
        ticket=ticket,
        run=run,
        manifest=manifest,
        budgets=budgets,
        policy=policy,
    )
    grant = await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.ticket_id == ticket.ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is not None:
        if not _grant_matches(grant, expected):
            _revoke(grant, now=now)
            raise CodingInferenceGrantIntegrityError(
                "stored coding inference grant drifted"
            )
        if grant.status in {"revoked", "exhausted"} or _aware(grant.expires_at) <= now:
            if grant.status != "revoked":
                _revoke(grant, now=now)
            raise CodingInferenceGrantNotAvailableError(
                "coding inference grant is terminal"
            )
        return CodingInferenceGrantResult(grant=grant, idempotent=True)

    grant = CodingInferenceGrant(
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
    session.add(grant)
    await session.flush()
    return CodingInferenceGrantResult(grant=grant, idempotent=False)


async def activate_coding_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    broker_public_key: str,
    policy: CodingInferencePolicy,
) -> CodingInferenceGrantActivation:
    """Rotate a live grant to one broker key and return a fresh opaque bearer."""

    try:
        policy = _canonical_policy(policy)
    except ValueError as error:
        raise CodingInferenceGrantIntegrityError(
            "coding inference policy is malformed"
        ) from error
    normalized_broker_key = broker_public_key.rstrip("=")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", normalized_broker_key) is None:
        raise CodingInferenceGrantIntegrityError(
            "coding inference broker key is malformed"
        )
    snapshot = await session.get(CodingInferenceGrant, grant_id)
    if snapshot is None or snapshot.validator_hotkey != validator_hotkey:
        raise CodingInferenceGrantNotAvailableError(
            "coding inference grant is unavailable"
        )
    ticket = await session.get(
        CodingShadowTicket,
        snapshot.ticket_id,
        with_for_update=True,
    )
    grant = await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    run = (
        await session.get(CodingShadowRun, ticket.run_row_id)
        if ticket is not None
        else None
    )
    now = await _database_now(session)
    if (
        grant is None
        or ticket is None
        or run is None
        or grant.validator_hotkey != validator_hotkey
        or grant.ticket_id != ticket.ticket_id
        or grant.run_row_id != ticket.run_row_id
        or _aware(grant.expires_at) != _aware(ticket.deadline)
        or grant.task_count != ticket.task_count
        or grant.inference_grant_sha256 != run.inference_grant_sha256
        or grant.inference_grant_sha256 != policy_digest(policy)
        or grant.model != policy.model
        or grant.provider_api != policy.provider_api
        or grant.provider_route != policy.provider_route
        or grant.receipt_provider != policy.receipt_provider
        or grant.provider_route_profile != policy.provider_route_profile
        or grant.provider_account_guardrail != policy.provider_account_guardrail
        or grant.provider_pipeline_policy != policy.provider_pipeline_policy
        or grant.provider_cache_policy != policy.provider_cache_policy
        or grant.reasoning_effort != policy.reasoning_effort
        or grant.request_budget > policy.max_requests
        or grant.prompt_token_budget > policy.max_prompt_tokens
        or grant.completion_token_budget > policy.max_completion_tokens
        or grant.cost_budget_usd_micros > policy.max_cost_usd_micros
        or grant.weight_eligible is not False
        or grant.status in {"revoked", "exhausted"}
        or grant.active_requests != 0
        or grant.generation >= (1 << 31) - 1
        or not await _authoring_is_open(session, ticket=ticket, run=run, now=now)
    ):
        if grant is not None and grant.status not in {"revoked", "exhausted"}:
            _revoke(grant, now=now)
        raise CodingInferenceGrantNotAvailableError(
            "coding inference grant is not live"
        )
    bearer = secrets.token_urlsafe(32)
    revoke_bearer = secrets.token_urlsafe(32)
    while revoke_bearer == bearer:  # pragma: no cover - cryptographic collision
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
        grant=grant,
        bearer=bearer,
        revoke_bearer=revoke_bearer,
    )


async def revoke_coding_inference_grant_by_capability(
    session: AsyncSession,
    *,
    grant_id: UUID,
    ticket_id: UUID,
    generation: int,
    revoke_bearer: str,
) -> CodingInferenceGrantRevocation:
    """Idempotently revoke one active generation through its narrow bearer."""

    if (
        generation < 1
        or generation > (1 << 31) - 1
        or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", revoke_bearer) is None
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding inference revoke capability is malformed"
        )
    observed_digest = coding_inference_bearer_digest(revoke_bearer)
    snapshot = await session.get(CodingInferenceGrant, grant_id)
    if (
        snapshot is None
        or snapshot.ticket_id != ticket_id
        or snapshot.generation != generation
        or snapshot.revoke_bearer_digest is None
        or not hmac.compare_digest(snapshot.revoke_bearer_digest, observed_digest)
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding inference grant is unavailable"
        )
    ticket = await session.get(
        CodingShadowTicket,
        ticket_id,
        with_for_update=True,
    )
    grant = await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await _database_now(session)
    if (
        ticket is None
        or grant is None
        or grant.ticket_id != ticket.ticket_id
        or grant.generation != generation
        or grant.revoke_bearer_digest is None
        or not hmac.compare_digest(grant.revoke_bearer_digest, observed_digest)
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding inference revoke capability is unavailable"
        )
    if grant.status == "revoked":
        return CodingInferenceGrantRevocation(grant=grant, idempotent=True)
    if grant.status != "active":
        raise CodingInferenceGrantConflictError("coding inference grant is not active")
    if grant.active_requests != 0:
        raise CodingInferenceGrantConflictError(
            "coding inference grant still has an active request"
        )
    _revoke(grant, now=now)
    await session.flush()
    return CodingInferenceGrantRevocation(grant=grant, idempotent=False)


async def revoke_coding_inference_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    validator_hotkey: str,
    generation: int,
) -> CodingInferenceGrantRevocation:
    """Durably revoke exactly the caller's observed grant generation."""

    snapshot = await session.get(CodingInferenceGrant, grant_id)
    if snapshot is None or snapshot.validator_hotkey != validator_hotkey:
        raise CodingInferenceGrantNotAvailableError(
            "coding inference grant is unavailable"
        )
    ticket = await session.get(
        CodingShadowTicket,
        snapshot.ticket_id,
        with_for_update=True,
    )
    grant = await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = await _database_now(session)
    if (
        ticket is None
        or grant is None
        or grant.validator_hotkey != validator_hotkey
        or grant.ticket_id != ticket.ticket_id
    ):
        raise CodingInferenceGrantNotAvailableError(
            "coding inference grant is unavailable"
        )
    if grant.generation != generation:
        raise CodingInferenceGrantConflictError(
            "coding inference grant generation changed"
        )
    if grant.status == "revoked":
        return CodingInferenceGrantRevocation(grant=grant, idempotent=True)
    if grant.status == "exhausted":
        raise CodingInferenceGrantConflictError("coding inference grant is exhausted")
    _revoke(grant, now=now)
    await session.flush()
    return CodingInferenceGrantRevocation(grant=grant, idempotent=False)


async def revoke_ticket_coding_inference(
    session: AsyncSession,
    *,
    ticket_id: UUID,
) -> bool:
    """Close authoring inference inside a freeze/result transaction."""

    ticket = await session.get(CodingShadowTicket, ticket_id, with_for_update=True)
    if ticket is None:
        raise CodingInferenceGrantNotAvailableError(
            "coding inference ticket is unavailable"
        )
    grant = await session.scalar(
        select(CodingInferenceGrant)
        .where(CodingInferenceGrant.ticket_id == ticket_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is None or grant.status in {"revoked", "exhausted"}:
        return False
    now = await _database_now(session)
    _revoke(grant, now=now)
    await session.flush()
    return True


__all__ = [
    "CodingInferenceGrantConflictError",
    "CodingInferenceGrantActivation",
    "CodingInferenceGrantIntegrityError",
    "CodingInferenceGrantNotAvailableError",
    "CodingInferenceGrantResult",
    "CodingInferenceGrantRevocation",
    "activate_coding_inference_grant",
    "coding_inference_bearer_digest",
    "ensure_coding_inference_grant",
    "revoke_coding_inference_grant",
    "revoke_coding_inference_grant_by_capability",
    "revoke_ticket_coding_inference",
]
