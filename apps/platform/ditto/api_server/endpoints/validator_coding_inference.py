"""Signed shadow coding inference grant offer, exchange, and revocation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    parse_coding_inference_json,
    policy_digest,
)
from ditto.api_models.coding_inference_grants import (
    CodingInferenceCapabilityRevokeRequest,
    CodingInferenceExchangeRequest,
    CodingInferenceExchangeResponse,
    CodingInferenceGrantOffer,
    CodingInferenceGrantRequest,
    CodingInferenceRevokeRequest,
    CodingInferenceRevokeResponse,
    coding_inference_exchange_signing_message,
    coding_inference_grant_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.coding_selection import (
    CodingSelectionCatalogIntegrityError,
    CodingSelectionCatalogUnavailableError,
)
from ditto.db.models import CodingInferenceGrant
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantConflictError,
    CodingInferenceGrantIntegrityError,
    CodingInferenceGrantNotAvailableError,
    activate_coding_inference_grant,
    ensure_coding_inference_grant,
    revoke_coding_inference_grant,
    revoke_coding_inference_grant_by_capability,
)
from ditto.db.queries.coding_task_leases import (
    CodingTaskLeaseIntegrityError,
    CodingTaskLeaseNotAvailableError,
    authorize_coding_shadow_task_delivery,
    build_coding_shadow_task_lease,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]

_REQUEST_MAX_AGE = timedelta(minutes=5)
_LOCKED_POLICY_SHA256 = (
    "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
)


@dataclass(frozen=True)
class CodingInferenceGrantTransport:
    """Explicitly injected shadow transport; absent means the feature is off."""

    policy: CodingInferencePolicy
    exchange_url: str
    proxy_url: str
    revoke_url: str


def coding_inference_transport_from_env(
    environ: Mapping[str, str] | None = None,
) -> CodingInferenceGrantTransport | None:
    """Load the complete locked transport only behind the explicit feature gate."""

    values = os.environ if environ is None else environ
    if values.get("DITTO_CODING_SHADOW_ENABLED", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return None
    path = values.get("DITTO_CODING_INFERENCE_POLICY_FILE", "").strip()
    exchange_url = values.get("DITTO_CODING_INFERENCE_EXCHANGE_URL", "").strip()
    proxy_url = values.get("DITTO_CODING_INFERENCE_PROXY_URL", "").strip()
    revoke_url = values.get("DITTO_CODING_INFERENCE_REVOKE_URL", "").strip()
    if not all((path, exchange_url, proxy_url, revoke_url)):
        raise ValueError("enabled coding inference transport is incomplete")
    policy_path = Path(path)
    if not policy_path.is_absolute():
        raise ValueError("coding inference policy path must be absolute")
    try:
        body = policy_path.read_bytes()
        policy = parse_coding_inference_json(CodingInferencePolicy, body)
    except OSError as error:
        raise ValueError("coding inference policy file is unavailable") from error
    if policy_digest(policy) != _LOCKED_POLICY_SHA256:
        raise ValueError("coding inference policy digest is not locked")
    return _validated_transport(
        CodingInferenceGrantTransport(
            policy=policy,
            exchange_url=exchange_url,
            proxy_url=proxy_url,
            revoke_url=revoke_url,
        )
    )


def _validated_transport(
    value: CodingInferenceGrantTransport,
) -> CodingInferenceGrantTransport:
    policy = CodingInferencePolicy.model_validate_json(
        value.policy.model_dump_json(by_alias=True)
    )
    urls = (
        (
            value.exchange_url,
            "/api/v1/validator/coding-shadow/inference-exchange",
        ),
        (value.proxy_url, "/api/v1/inference/coding/chat/completions"),
        (
            value.revoke_url,
            "/api/v1/validator/coding-shadow/inference-revoke-capability",
        ),
    )
    for url, suffix in urls:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.endswith(suffix)
        ):
            raise ValueError("invalid coding inference transport URL")
    if policy != value.policy:
        raise ValueError("coding inference grant policy is invalid")
    return value


def _transport(request: Request) -> CodingInferenceGrantTransport:
    value = getattr(request.app.state, "coding_inference_grant_transport", None)
    if not isinstance(value, CodingInferenceGrantTransport):
        raise HTTPException(
            status_code=503,
            detail="coding inference grants are not configured",
        )
    try:
        return _validated_transport(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=503,
            detail="coding inference grant transport is invalid",
        ) from None


def _fresh(value: datetime) -> bool:
    return timedelta(0) <= datetime.now(UTC) - value.astimezone(UTC) <= _REQUEST_MAX_AGE


async def _permitted(
    request: Request,
    chain: ChainClient,
    validator_hotkey: str,
) -> None:
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )


async def _consume_nonce(
    session: AsyncSession,
    *,
    nonce: UUID,
    validator_hotkey: str,
    now: datetime,
    requested_at: datetime,
) -> None:
    try:
        await consume_validator_nonce(
            session,
            nonce=nonce,
            validator_hotkey=validator_hotkey,
            now=now,
            expires_at=requested_at.astimezone(UTC) + _REQUEST_MAX_AGE,
        )
    except ValidatorRequestReplayError:
        raise HTTPException(
            status_code=409,
            detail="coding inference request nonce has already been used",
        ) from None


def _authority(grant: CodingInferenceGrant) -> dict[str, object]:
    return {
        "coding_contract_version": 1,
        "weight_eligible": False,
        "grant_id": grant.grant_id,
        "ticket_id": grant.ticket_id,
        "run_row_id": grant.run_row_id,
        "case_id": grant.case_id,
        "profile_capability_id": grant.profile_capability_id,
        "inference_grant_sha256": grant.inference_grant_sha256,
        "model": grant.model,
        "provider_api": grant.provider_api,
        "provider_route": grant.provider_route,
        "receipt_provider": grant.receipt_provider,
        "provider_route_profile": grant.provider_route_profile,
        "provider_account_guardrail": grant.provider_account_guardrail,
        "provider_pipeline_policy": grant.provider_pipeline_policy,
        "provider_cache_policy": grant.provider_cache_policy,
        "reasoning_effort": grant.reasoning_effort,
        "request_budget": grant.request_budget,
        "prompt_token_budget": grant.prompt_token_budget,
        "completion_token_budget": grant.completion_token_budget,
        "cost_budget_usd_micros": grant.cost_budget_usd_micros,
        "expires_at": grant.expires_at,
    }


@router.post(
    "/coding-shadow/inference-grant",
    response_model=CodingInferenceGrantOffer,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Coding shadow ticket is unavailable."},
        409: {"description": "Replay or immutable authority conflict."},
        503: {"description": "Private task or grant transport unavailable."},
    },
)
async def request_coding_inference_grant(
    payload: CodingInferenceGrantRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingInferenceGrantOffer:
    """Mint or replay the one pending/active grant for a private coding task."""

    response.headers["Cache-Control"] = "no-store"
    transport = _transport(request)
    if not _fresh(payload.requested_at):
        raise HTTPException(status_code=409, detail="coding inference request is stale")
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=coding_inference_grant_signing_message(
            validator_hotkey=payload.validator_hotkey,
            ticket_id=payload.ticket_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding inference grant signature did not verify")
    await _permitted(request, chain, payload.validator_hotkey)
    material_source = getattr(request.app.state, "coding_private_catalog_source", None)
    if material_source is None:
        raise HTTPException(
            status_code=503,
            detail="coding private catalog is unavailable",
        )
    now = datetime.now(UTC)
    async with session.begin():
        await _consume_nonce(
            session,
            nonce=payload.nonce,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            requested_at=payload.requested_at,
        )
        try:
            await authorize_coding_shadow_task_delivery(
                session,
                ticket_id=payload.ticket_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingTaskLeaseNotAvailableError:
            raise HTTPException(
                status_code=404,
                detail="coding shadow ticket is unavailable",
            ) from None
    try:
        lease = await build_coding_shadow_task_lease(
            session,
            ticket_id=payload.ticket_id,
            material_source=material_source,
        )
    except CodingTaskLeaseNotAvailableError:
        raise HTTPException(
            status_code=404,
            detail="coding shadow ticket is unavailable",
        ) from None
    except (
        CodingTaskLeaseIntegrityError,
        CodingSelectionCatalogIntegrityError,
    ):
        raise HTTPException(
            status_code=409,
            detail="coding inference task authority is inconsistent",
        ) from None
    except CodingSelectionCatalogUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="coding private catalog is temporarily unavailable",
        ) from None
    finally:
        if session.in_transaction():
            await session.rollback()
    result = None
    grant_error: Exception | None = None
    async with session.begin():
        try:
            result = await ensure_coding_inference_grant(
                session,
                lease=lease,
                policy=transport.policy,
            )
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantIntegrityError,
        ) as error:
            grant_error = error
    if isinstance(grant_error, CodingInferenceGrantNotAvailableError):
        raise HTTPException(
            status_code=404,
            detail="coding inference grant is unavailable",
        )
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding inference grant authority is inconsistent",
        )
    if result is None:  # pragma: no cover - exhaustive typed outcomes
        raise HTTPException(
            status_code=503,
            detail="coding inference grant result is unavailable",
        )
    try:
        return CodingInferenceGrantOffer.model_validate(
            {
                "schema": "dittobench-coding-inference-grant-offer-v1",
                **_authority(result.grant),
                "status": result.grant.status,
                "generation": result.grant.generation,
                "exchange_url": transport.exchange_url,
            }
        )
    except ValidationError:
        raise HTTPException(
            status_code=503,
            detail="coding inference grant transport is invalid",
        ) from None


@router.post(
    "/coding-shadow/inference-exchange",
    response_model=CodingInferenceExchangeResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        409: {"description": "Replay, expiry, or grant authority conflict."},
        503: {"description": "Coding grant transport unavailable."},
    },
)
async def exchange_coding_inference_grant(
    payload: CodingInferenceExchangeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingInferenceExchangeResponse:
    """Rotate a live coding grant onto one validator broker key."""

    response.headers["Cache-Control"] = "no-store"
    transport = _transport(request)
    if not _fresh(payload.requested_at):
        raise HTTPException(
            status_code=409, detail="coding inference exchange is stale"
        )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=coding_inference_exchange_signing_message(
            validator_hotkey=payload.validator_hotkey,
            grant_id=payload.grant_id,
            broker_public_key=payload.broker_public_key,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding inference exchange signature did not verify")
    await _permitted(request, chain, payload.validator_hotkey)
    now = datetime.now(UTC)
    activated = None
    grant_error: Exception | None = None
    async with session.begin():
        await _consume_nonce(
            session,
            nonce=payload.nonce,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            requested_at=payload.requested_at,
        )
        try:
            activated = await activate_coding_inference_grant(
                session,
                grant_id=payload.grant_id,
                validator_hotkey=payload.validator_hotkey,
                broker_public_key=payload.broker_public_key,
                policy=transport.policy,
            )
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantIntegrityError,
        ) as error:
            grant_error = error
    if grant_error is not None:
        detail = (
            "coding inference grant is not live"
            if isinstance(grant_error, CodingInferenceGrantNotAvailableError)
            else "coding inference grant authority is inconsistent"
        )
        raise HTTPException(status_code=409, detail=detail)
    if activated is None:  # pragma: no cover - exhaustive typed outcomes
        raise HTTPException(
            status_code=503,
            detail="coding inference exchange result is unavailable",
        )
    grant = activated.grant
    try:
        return CodingInferenceExchangeResponse.model_validate(
            {
                "schema": "dittobench-coding-inference-exchange-v1",
                **_authority(grant),
                "status": "active",
                "generation": grant.generation,
                "bearer": activated.bearer,
                "proxy_url": transport.proxy_url,
                "revoke_bearer": activated.revoke_bearer,
                "revoke_url": transport.revoke_url,
            }
        )
    except ValidationError:
        raise HTTPException(
            status_code=503,
            detail="coding inference exchange transport is invalid",
        ) from None


@router.post(
    "/coding-shadow/inference-revoke-capability",
    response_model=CodingInferenceRevokeResponse,
    responses={
        401: {"description": "Revocation capability is missing or invalid."},
        404: {"description": "Coding inference grant unavailable."},
        409: {"description": "Grant generation or state conflicts."},
        503: {"description": "Coding inference transport unavailable."},
    },
)
async def revoke_coding_inference_grant_capability_endpoint(
    payload: CodingInferenceCapabilityRevokeRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CodingInferenceRevokeResponse:
    """Revoke one active generation through its revocation-only bearer."""

    response.headers["Cache-Control"] = "no-store"
    _transport(request)
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="revoke capability is missing")
    bearer = authorization.removeprefix("Bearer ")
    if not bearer or bearer != bearer.strip():
        raise HTTPException(status_code=401, detail="revoke capability is invalid")
    revoked = None
    grant_error: Exception | None = None
    async with session.begin():
        try:
            revoked = await revoke_coding_inference_grant_by_capability(
                session,
                grant_id=payload.grant_id,
                ticket_id=payload.ticket_id,
                generation=payload.generation,
                revoke_bearer=bearer,
            )
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantConflictError,
        ) as error:
            grant_error = error
    if isinstance(grant_error, CodingInferenceGrantNotAvailableError):
        raise HTTPException(
            status_code=404,
            detail="coding inference revoke capability is unavailable",
        )
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding inference grant is not revocable",
        )
    if revoked is None or revoked.grant.revoked_at is None:
        raise HTTPException(
            status_code=503,
            detail="coding inference revocation result is unavailable",
        )
    return CodingInferenceRevokeResponse(
        schema="dittobench-coding-inference-revocation-v1",
        coding_contract_version=1,
        weight_eligible=False,
        grant_id=revoked.grant.grant_id,
        ticket_id=revoked.grant.ticket_id,
        status="revoked",
        generation=revoked.grant.generation,
        revoked_at=revoked.grant.revoked_at,
        idempotent=revoked.idempotent,
    )


@router.post(
    "/coding-shadow/inference-revoke",
    response_model=CodingInferenceRevokeResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Coding inference grant unavailable."},
        409: {"description": "Replay or generation conflict."},
    },
)
async def revoke_coding_inference_grant_endpoint(
    payload: CodingInferenceRevokeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingInferenceRevokeResponse:
    """Durably revoke exactly the validator's observed grant generation."""

    response.headers["Cache-Control"] = "no-store"
    if not _fresh(payload.requested_at):
        raise HTTPException(
            status_code=409, detail="coding inference revocation is stale"
        )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=coding_inference_revoke_signing_message(
            validator_hotkey=payload.validator_hotkey,
            grant_id=payload.grant_id,
            generation=payload.generation,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding inference revocation signature did not verify")
    await _permitted(request, chain, payload.validator_hotkey)
    now = datetime.now(UTC)
    revoked = None
    grant_error: Exception | None = None
    async with session.begin():
        await _consume_nonce(
            session,
            nonce=payload.nonce,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            requested_at=payload.requested_at,
        )
        try:
            revoked = await revoke_coding_inference_grant(
                session,
                grant_id=payload.grant_id,
                validator_hotkey=payload.validator_hotkey,
                generation=payload.generation,
            )
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantConflictError,
        ) as error:
            grant_error = error
    if isinstance(grant_error, CodingInferenceGrantNotAvailableError):
        raise HTTPException(
            status_code=404,
            detail="coding inference grant is unavailable",
        )
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding inference grant generation changed",
        )
    if revoked is None:  # pragma: no cover - exhaustive typed outcomes
        raise HTTPException(
            status_code=503,
            detail="coding inference revocation result is unavailable",
        )
    if revoked.grant.revoked_at is None:  # pragma: no cover - state invariant
        raise HTTPException(
            status_code=409,
            detail="coding inference revocation was not durable",
        )
    return CodingInferenceRevokeResponse(
        schema="dittobench-coding-inference-revocation-v1",
        coding_contract_version=1,
        weight_eligible=False,
        grant_id=revoked.grant.grant_id,
        ticket_id=revoked.grant.ticket_id,
        status="revoked",
        generation=revoked.grant.generation,
        revoked_at=revoked.grant.revoked_at,
        idempotent=revoked.idempotent,
    )


__all__ = [
    "CodingInferenceGrantTransport",
    "coding_inference_transport_from_env",
    "router",
]
