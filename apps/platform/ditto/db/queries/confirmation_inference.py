"""Purpose-bound Platform inference for one Bench v9 confirmation ticket."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from ditto.api_server.confirmation_evidence import ConfirmationVerificationProfile
from ditto.db.models import (
    ConfirmationBundleTicket,
    ConfirmationInferenceGrant,
    ConfirmationInferenceRequest,
)


class ConfirmationInferenceDecline(StrEnum):
    UNATTRIBUTED = "unattributed"
    LEASE_EXPIRED = "lease_expired"
    GRANT_REVOKED = "grant_revoked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    MODEL_NOT_PERMITTED = "model_not_permitted"
    NONCE_REPLAYED = "nonce_replayed"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _bearer_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cancel_unsettled_confirmation_request(
    request: ConfirmationInferenceRequest, now: datetime
) -> None:
    """Drop an in-flight confirmation request without booking the estimate."""
    request.status = "canceled"
    request.prompt_tokens = 0
    request.completed_at = now


def _lane_specs(
    profile: ConfirmationVerificationProfile,
) -> dict[str, tuple[str, str, str, str, str, int, int, int]]:
    specs = {
        lane.lane: (
            lane.model,
            lane.provider,
            lane.route_provider,
            lane.receipt_provider,
            lane.profile_revision,
            lane.max_requests,
            lane.max_total_tokens,
            lane.max_cost_usd_micros,
        )
        for lane in profile.provider_lanes
    }
    embedding = profile.embedding_lane
    specs[embedding.lane] = (
        embedding.model,
        embedding.provider,
        embedding.provider,
        embedding.provider,
        embedding.profile_revision,
        embedding.max_requests,
        embedding.max_input_tokens,
        embedding.max_cost_usd_micros,
    )
    if set(specs) != {"reader", "judge", "embedding"}:
        raise ValueError("confirmation profile must freeze three inference lanes")
    return specs


async def ensure_confirmation_inference_grants(
    session,
    *,
    ticket: ConfirmationBundleTicket,
    broker_public_key: str,
    profile: ConfirmationVerificationProfile,
    now: datetime,
) -> list[tuple[ConfirmationInferenceGrant, str]]:
    """Mint or rotate the exact three live ticket capabilities.

    Rotation is resume-safe and never resets durable spend. Every prior bearer
    is invalidated, while the ticket, lane, budgets, and frozen route remain the
    same immutable authority.
    """
    if ticket.status != "issued" or _aware(ticket.deadline) <= now:
        return []
    grants: list[tuple[ConfirmationInferenceGrant, bool]] = []
    for lane, spec in sorted(_lane_specs(profile).items()):
        (
            model,
            provider,
            route_provider,
            receipt_provider,
            revision,
            request_budget,
            token_budget,
            cost_budget,
        ) = spec
        grant = await session.scalar(
            select(ConfirmationInferenceGrant)
            .where(
                ConfirmationInferenceGrant.ticket_id == ticket.ticket_id,
                ConfirmationInferenceGrant.lane == lane,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        created = grant is None
        if grant is None:
            grant = ConfirmationInferenceGrant(
                grant_id=uuid4(),
                ticket_id=ticket.ticket_id,
                bundle_id=ticket.bundle_id,
                validator_hotkey=ticket.validator_hotkey,
                lane=lane,
                status="active",
                # The row must satisfy its fail-closed DB invariants before a
                # later SELECT triggers autoflush. This placeholder is never
                # returned and is replaced with a fresh random bearer below.
                bearer_digest="0" * 64,
                broker_public_key=broker_public_key.rstrip("="),
                generation=1,
                model=model,
                provider=provider,
                route_provider=route_provider,
                receipt_provider=receipt_provider,
                profile_revision=revision,
                request_budget=request_budget,
                token_budget=token_budget,
                cost_budget_microusd=cost_budget,
                request_count=0,
                prompt_tokens=0,
                completion_tokens=0,
                cost_microusd=0,
                active_requests=0,
                expires_at=_aware(ticket.deadline),
                created_at=now,
                updated_at=now,
            )
            session.add(grant)
        expected = (
            model,
            provider,
            route_provider,
            receipt_provider,
            revision,
            request_budget,
            token_budget,
            cost_budget,
        )
        observed = (
            grant.model,
            grant.provider,
            grant.route_provider,
            grant.receipt_provider,
            grant.profile_revision,
            grant.request_budget,
            grant.token_budget,
            grant.cost_budget_microusd,
        )
        if observed != expected or grant.bundle_id != ticket.bundle_id:
            grant.status = "revoked"
            raise ValueError("confirmation grant drifted from its frozen profile")
        grants.append((grant, created))

    # Lock and inspect the complete three-lane request set before rotating any
    # bearer.  Returning after mutating the first lane would strand a resumed
    # ticket with a mixed generation if a later lane still had a live request.
    active_by_grant: dict[UUID, list[ConfirmationInferenceRequest]] = {}
    stale_cutoff = now - timedelta(minutes=4)
    for grant, _created in grants:
        active = list(
            await session.scalars(
                select(ConfirmationInferenceRequest)
                .where(
                    ConfirmationInferenceRequest.grant_id == grant.grant_id,
                    ConfirmationInferenceRequest.status == "started",
                )
                .with_for_update()
            )
        )
        if any(_aware(request.started_at) >= stale_cutoff for request in active):
            return []
        active_by_grant[grant.grant_id] = active

    offers: list[tuple[ConfirmationInferenceGrant, str]] = []
    for grant, created in grants:
        active = active_by_grant[grant.grant_id]
        for request in active:
            _cancel_unsettled_confirmation_request(request, now)
        bearer = secrets.token_urlsafe(32)
        grant.bearer_digest = _bearer_digest(bearer)
        grant.broker_public_key = broker_public_key.rstrip("=")
        if not created:
            grant.generation += 1
        grant.status = "active"
        grant.active_requests = 0
        grant.expires_at = _aware(ticket.deadline)
        grant.updated_at = now
        await session.flush()
        offers.append((grant, bearer))
    return offers


async def begin_confirmation_inference_request(
    session,
    *,
    grant_id: UUID,
    nonce: UUID,
    bearer: str,
    model: str,
    token_reservation: int,
    max_chargeable_tokens: int,
    now: datetime,
) -> (
    tuple[ConfirmationInferenceGrant, ConfirmationInferenceRequest]
    | ConfirmationInferenceDecline
):
    snapshot = await session.get(ConfirmationInferenceGrant, grant_id)
    if snapshot is None:
        return ConfirmationInferenceDecline.UNATTRIBUTED
    ticket = await session.get(
        ConfirmationBundleTicket, snapshot.ticket_id, with_for_update=True
    )
    grant = await session.scalar(
        select(ConfirmationInferenceGrant)
        .where(ConfirmationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        grant is None
        or not grant.bearer_digest
        or not secrets.compare_digest(grant.bearer_digest, _bearer_digest(bearer))
    ):
        return ConfirmationInferenceDecline.UNATTRIBUTED
    if grant.status == "revoked":
        return ConfirmationInferenceDecline.GRANT_REVOKED
    if grant.status == "exhausted":
        return ConfirmationInferenceDecline.BUDGET_EXHAUSTED
    if ticket is None or ticket.status != "issued" or _aware(ticket.deadline) <= now:
        grant.status = "revoked"
        return ConfirmationInferenceDecline.LEASE_EXPIRED
    if model != grant.model:
        return ConfirmationInferenceDecline.MODEL_NOT_PERMITTED
    request = await session.scalar(
        postgresql_insert(ConfirmationInferenceRequest)
        .values(
            grant_id=grant_id,
            nonce=nonce,
            generation=grant.generation,
            status="started",
            model=model,
            # Keep the provisional row constraint-valid so replay detection
            # stays ahead of malformed-reservation classification exactly as
            # before. A malformed fresh request deletes this row below.
            reserved_tokens=max(1, token_reservation),
            max_chargeable_tokens=max(1, token_reservation, max_chargeable_tokens),
            prompt_tokens=0,
            completion_tokens=0,
            cost_microusd=0,
            started_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=(
                ConfirmationInferenceRequest.grant_id,
                ConfirmationInferenceRequest.nonce,
            )
        )
        .returning(ConfirmationInferenceRequest)
    )
    if request is None:
        return ConfirmationInferenceDecline.NONCE_REPLAYED
    if grant.request_count >= grant.request_budget:
        grant.status = "exhausted"
        await session.delete(request)
        return ConfirmationInferenceDecline.BUDGET_EXHAUSTED
    if token_reservation < 1 or max_chargeable_tokens < token_reservation:
        await session.delete(request)
        return ConfirmationInferenceDecline.UNATTRIBUTED
    if grant.prompt_tokens + grant.completion_tokens >= grant.token_budget:
        grant.status = "exhausted"
        await session.delete(request)
        return ConfirmationInferenceDecline.TOKEN_BUDGET_EXHAUSTED
    if grant.cost_microusd >= grant.cost_budget_microusd > 0:
        grant.status = "exhausted"
        await session.delete(request)
        return ConfirmationInferenceDecline.COST_BUDGET_EXHAUSTED
    grant.request_count += 1
    grant.active_requests += 1
    grant.updated_at = now
    await session.flush()
    return grant, request


async def finish_confirmation_inference_request(
    session,
    *,
    grant_id: UUID,
    nonce: UUID,
    generation: int,
    status: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_microusd: int,
    upstream_provider: str | None,
    now: datetime,
) -> bool:
    grant = await session.scalar(
        select(ConfirmationInferenceGrant)
        .where(ConfirmationInferenceGrant.grant_id == grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    request = await session.get(
        ConfirmationInferenceRequest, (grant_id, nonce), with_for_update=True
    )
    if (
        grant is None
        or request is None
        or request.status != "started"
        or request.generation != generation
    ):
        return False
    if grant.lane == "judge":
        provider_matches = upstream_provider in {None, grant.receipt_provider}
    else:
        provider_matches = upstream_provider is None or (
            isinstance(upstream_provider, str) and 1 <= len(upstream_provider) <= 120
        )
    cost_fits = grant.cost_microusd + cost_microusd <= grant.cost_budget_microusd
    usage_valid = (
        prompt_tokens >= 0
        and completion_tokens >= 0
        and cost_microusd >= 0
        and prompt_tokens + completion_tokens <= request.max_chargeable_tokens
        and provider_matches
        and cost_fits
    )
    delivered = status == "completed" and usage_valid
    if not delivered:
        prompt_tokens = 0
        completion_tokens = 0
        cost_microusd = 0
        status = "failed"
    request.status = status
    request.prompt_tokens = prompt_tokens
    request.completion_tokens = completion_tokens
    request.cost_microusd = cost_microusd
    request.upstream_provider = upstream_provider
    request.completed_at = now
    grant.prompt_tokens += prompt_tokens
    grant.completion_tokens += completion_tokens
    grant.cost_microusd += cost_microusd
    grant.active_requests = max(0, grant.active_requests - 1)
    if (
        grant.request_count >= grant.request_budget
        or grant.prompt_tokens + grant.completion_tokens >= grant.token_budget
        or (
            grant.cost_budget_microusd > 0
            and grant.cost_microusd >= grant.cost_budget_microusd
        )
    ):
        grant.status = "exhausted"
    grant.updated_at = now
    await session.flush()
    return delivered


__all__ = [
    "ConfirmationInferenceDecline",
    "begin_confirmation_inference_request",
    "ensure_confirmation_inference_grants",
    "finish_confirmation_inference_request",
]
