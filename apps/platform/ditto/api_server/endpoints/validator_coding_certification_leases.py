"""Validator issue, claim, and abort for shadow coding-certification leases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchRequest,
    CodingCertificationHarnessLaunchResponse,
    CodingCertificationLeaseAbortRequest,
    CodingCertificationLeaseClaimRequest,
    CodingCertificationLeaseIssueRequest,
    CodingCertificationLeaseResponse,
    CodingCertificationLeaseStatus,
    coding_certification_harness_launch_signing_message,
    coding_certification_lease_abort_signing_message,
    coding_certification_lease_claim_signing_message,
    coding_certification_lease_issue_signing_message,
)
from ditto.api_models.coding_inference_grants import (
    CodingCertificationInferenceExchangeResponse,
    CodingCertificationInferenceGrantOffer,
    CodingCertificationInferenceGrantRequest,
    CodingCertificationInferenceRevokeResponse,
    CodingInferenceExchangeRequest,
    CodingInferenceRevokeRequest,
    coding_certification_inference_grant_signing_message,
    coding_inference_exchange_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import (
    get_chain_client,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints.validator import (
    _ARTIFACT_URL_TTL,
    ValidatorAuthError,
    _assert_validator_permitted,
    _screened_image_key,
)
from ditto.api_server.endpoints.validator_coding_inference import _transport
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainClient
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_VALIDATOR_CODING_CERTIFICATION_HARNESS,
    record_artifact_fetch,
)
from ditto.db.queries.coding_certification_inference_grants import (
    activate_coding_certification_inference_grant,
    ensure_coding_certification_inference_grant,
    revoke_coding_certification_inference_grant,
)
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    CodingCertificationLeaseResult,
    CodingCertificationLeaseUnavailableError,
    abort_coding_certification_lease,
    authorize_coding_certification_harness_delivery,
    claim_coding_certification_lease,
    issue_coding_certification_lease,
)
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantConflictError,
    CodingInferenceGrantIntegrityError,
    CodingInferenceGrantNotAvailableError,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
_REQUEST_MAX_AGE = timedelta(minutes=5)
_NO_STORE = {"Cache-Control": "no-store"}


@router.post(
    "/coding-certification-leases",
    response_model=CodingCertificationLeaseResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Agent is not currently eligible."},
        409: {"description": "Replay or in-flight lease conflict."},
        503: {"description": "Public canary identity unavailable."},
    },
)
async def issue_lease(
    payload: CodingCertificationLeaseIssueRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationLeaseResponse:
    """Issue one public-canary lease after current core qualification."""

    response.headers["Cache-Control"] = "no-store"
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_certification_lease_issue_signing_message(
            validator_hotkey=payload.validator_hotkey,
            agent_id=payload.agent_id,
            bench_version=payload.bench_version,
            coding_contract_version=payload.coding_contract_version,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )

    async def _issue() -> CodingCertificationLeaseResult:
        return await issue_coding_certification_lease(
            session,
            validator_hotkey=payload.validator_hotkey,
            agent_id=payload.agent_id,
            bench_version=payload.bench_version,
            coding_contract_version=payload.coding_contract_version,
        )

    result = await _run_signed_lease_mutation(
        session=session,
        validator_hotkey=payload.validator_hotkey,
        nonce=payload.nonce,
        now=now,
        mutate=_issue,
        conflict_detail="coding certification lease already exists for this artifact",
    )
    return _response(result)


@router.post(
    "/coding-certification-leases/{lease_id}/claim",
    response_model=CodingCertificationLeaseResponse,
)
async def claim_lease(
    lease_id: UUID,
    payload: CodingCertificationLeaseClaimRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationLeaseResponse:
    response.headers["Cache-Control"] = "no-store"
    if payload.lease_id != lease_id:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease mismatch",
            headers=_NO_STORE,
        )
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_certification_lease_claim_signing_message(
            validator_hotkey=payload.validator_hotkey,
            lease_id=payload.lease_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )

    async def _claim() -> CodingCertificationLeaseResult:
        return await claim_coding_certification_lease(
            session,
            validator_hotkey=payload.validator_hotkey,
            lease_id=lease_id,
        )

    result = await _run_signed_lease_mutation(
        session=session,
        validator_hotkey=payload.validator_hotkey,
        nonce=payload.nonce,
        now=now,
        mutate=_claim,
        conflict_detail="coding certification lease already exists for this artifact",
    )
    return _response(result)


@router.post(
    "/coding-certification-leases/{lease_id}/abort",
    response_model=CodingCertificationLeaseResponse,
)
async def abort_lease(
    lease_id: UUID,
    payload: CodingCertificationLeaseAbortRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationLeaseResponse:
    response.headers["Cache-Control"] = "no-store"
    if payload.lease_id != lease_id:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease mismatch",
            headers=_NO_STORE,
        )
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_certification_lease_abort_signing_message(
            validator_hotkey=payload.validator_hotkey,
            lease_id=payload.lease_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )

    async def _abort() -> CodingCertificationLeaseResult:
        return await abort_coding_certification_lease(
            session,
            validator_hotkey=payload.validator_hotkey,
            lease_id=lease_id,
        )

    result = await _run_signed_lease_mutation(
        session=session,
        validator_hotkey=payload.validator_hotkey,
        nonce=payload.nonce,
        now=now,
        mutate=_abort,
        conflict_detail="claimed coding certification lease cannot be aborted",
    )
    return _response(result)


@router.post(
    "/coding-certification-leases/{lease_id}/harness-launch",
    response_model=CodingCertificationHarnessLaunchResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Coding certification harness unavailable."},
        409: {"description": "Replay, expiry, or immutable authority conflict."},
    },
)
async def request_coding_certification_harness_launch(
    lease_id: UUID,
    payload: CodingCertificationHarnessLaunchRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    storage: StorageDep,
) -> CodingCertificationHarnessLaunchResponse:
    """Return one short-lived screened image capability for a claimed lease."""

    response.headers["Cache-Control"] = "no-store"
    if payload.lease_id != lease_id:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease mismatch",
            headers=_NO_STORE,
        )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=coding_certification_harness_launch_signing_message(
            validator_hotkey=payload.validator_hotkey,
            lease_id=payload.lease_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError(
            "coding certification harness launch signature did not verify"
        )
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding certification harness launch request timestamp is stale",
            headers=_NO_STORE,
        )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    authority = None
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _REQUEST_MAX_AGE,
            )
        except ValidatorRequestReplayError:
            raise HTTPException(
                status_code=409,
                detail=(
                    "coding certification harness launch nonce has already been used"
                ),
                headers=_NO_STORE,
            ) from None
        try:
            authority = await authorize_coding_certification_harness_delivery(
                session,
                lease_id=payload.lease_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingCertificationLeaseNotAvailableError:
            raise HTTPException(
                status_code=404,
                detail="coding certification harness is unavailable",
                headers=_NO_STORE,
            ) from None
    if authority is None:  # pragma: no cover - exhaustive transaction outcome
        raise HTTPException(
            status_code=404,
            detail="coding certification harness is unavailable",
            headers=_NO_STORE,
        )
    issued_at = datetime.now(UTC)
    ttl_seconds = min(
        int(_ARTIFACT_URL_TTL.total_seconds()),
        int((authority.deadline - issued_at).total_seconds()),
    )
    if ttl_seconds < 1:
        raise HTTPException(
            status_code=409,
            detail="coding certification harness lease expired",
            headers=_NO_STORE,
        )
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    image_url = await storage.presigned_get_url(
        key=_screened_image_key(
            authority.agent_id,
            authority.screened_image_upload_id,
        ),
        expires_in=ttl_seconds,
    )
    async with session.begin():
        try:
            refreshed = await authorize_coding_certification_harness_delivery(
                session,
                lease_id=payload.lease_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingCertificationLeaseNotAvailableError:
            raise HTTPException(
                status_code=409,
                detail=(
                    "coding certification harness authority changed after URL minting"
                ),
                headers=_NO_STORE,
            ) from None
    if refreshed != authority:
        raise HTTPException(
            status_code=409,
            detail="coding certification harness authority changed after URL minting",
            headers=_NO_STORE,
        )
    if datetime.now(UTC) >= expires_at:
        raise HTTPException(
            status_code=409,
            detail="coding certification harness URL expired",
            headers=_NO_STORE,
        )
    await record_artifact_fetch(
        session,
        agent_id=authority.agent_id,
        endpoint=ENDPOINT_VALIDATOR_CODING_CERTIFICATION_HARNESS,
        requester_kind="validator",
        requester_id=payload.validator_hotkey,
        lease_id=authority.lease_id,
        bench_version=authority.bench_version,
        artifact_sha256=authority.agent_artifact_sha256,
        source_ip=client_ip(request),
        detail=request_detail(
            request,
            served_screened_image=True,
            screened_image_sha256=authority.screened_image_sha256,
        ),
    )
    try:
        return CodingCertificationHarnessLaunchResponse(
            schema="dittobench-coding-certification-harness-launch-v1",
            coding_contract_version=1,
            weight_eligible=False,
            lease_id=authority.lease_id,
            agent_id=authority.agent_id,
            lease_deadline=authority.deadline,
            bench_version=authority.bench_version,
            agent_artifact_sha256=authority.agent_artifact_sha256,
            screened_image_sha256=authority.screened_image_sha256,
            screened_image_size_bytes=authority.screened_image_size_bytes,
            screened_image_id=authority.screened_image_id,
            screened_image_ref=authority.screened_image_ref,
            screening_policy_version=authority.screening_policy_version,
            image_url=image_url,
            expires_at=expires_at,
        )
    except ValidationError:
        raise HTTPException(
            status_code=409,
            detail="coding certification harness launch authority is inconsistent",
            headers=_NO_STORE,
        ) from None


_CANARY_EXCHANGE_SUFFIX = (
    "/api/v1/validator/coding-certification-leases/inference-exchange"
)
_TICKET_EXCHANGE_SUFFIX = "/api/v1/validator/coding-shadow/inference-exchange"


def _canary_exchange_url(exchange_url: str) -> str:
    if not exchange_url.endswith(_TICKET_EXCHANGE_SUFFIX):
        raise ValueError("coding certification inference exchange URL is invalid")
    return exchange_url[: -len(_TICKET_EXCHANGE_SUFFIX)] + _CANARY_EXCHANGE_SUFFIX


def _certification_grant_authority(grant: object) -> dict[str, object]:
    return {
        "coding_contract_version": 1,
        "weight_eligible": False,
        "grant_id": grant.grant_id,
        "lease_id": grant.lease_id,
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
    "/coding-certification-leases/{lease_id}/inference-grant",
    response_model=CodingCertificationInferenceGrantOffer,
)
async def request_coding_certification_inference_grant(
    lease_id: UUID,
    payload: CodingCertificationInferenceGrantRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationInferenceGrantOffer:
    """Mint or replay the one pending/active grant for a claimed canary lease."""

    response.headers["Cache-Control"] = "no-store"
    transport = _transport(request)
    if payload.lease_id != lease_id:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease mismatch",
            headers=_NO_STORE,
        )
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_certification_inference_grant_signing_message(
            validator_hotkey=payload.validator_hotkey,
            lease_id=payload.lease_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )
    result = None
    grant_error: Exception | None = None
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _REQUEST_MAX_AGE,
            )
            result = await ensure_coding_certification_inference_grant(
                session,
                lease_id=payload.lease_id,
                validator_hotkey=payload.validator_hotkey,
                policy=transport.policy,
            )
        except ValidatorRequestReplayError:
            raise HTTPException(
                status_code=409,
                detail="coding certification lease request replayed",
                headers=_NO_STORE,
            ) from None
        except CodingInferenceGrantNotAvailableError as error:
            grant_error = error
        except CodingInferenceGrantIntegrityError as error:
            grant_error = error
    if isinstance(grant_error, CodingInferenceGrantNotAvailableError):
        raise HTTPException(
            status_code=404,
            detail="coding certification inference grant is unavailable",
            headers=_NO_STORE,
        )
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding certification inference grant authority is inconsistent",
            headers=_NO_STORE,
        )
    if result is None:
        raise HTTPException(
            status_code=503,
            detail="coding certification inference grant result is unavailable",
            headers=_NO_STORE,
        )
    try:
        return CodingCertificationInferenceGrantOffer.model_validate(
            {
                "schema": "dittobench-coding-certification-inference-grant-offer-v1",
                **_certification_grant_authority(result.grant),
                "status": result.grant.status,
                "generation": result.grant.generation,
                "exchange_url": _canary_exchange_url(transport.exchange_url),
            }
        )
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(
            status_code=503,
            detail="coding certification inference transport is invalid",
            headers=_NO_STORE,
        ) from None


@router.post(
    "/coding-certification-leases/inference-exchange",
    response_model=CodingCertificationInferenceExchangeResponse,
)
async def exchange_coding_certification_inference_grant(
    payload: CodingInferenceExchangeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationInferenceExchangeResponse:
    """Rotate a live canary grant onto one validator broker key."""

    response.headers["Cache-Control"] = "no-store"
    transport = _transport(request)
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_inference_exchange_signing_message(
            validator_hotkey=payload.validator_hotkey,
            grant_id=payload.grant_id,
            broker_public_key=payload.broker_public_key,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )
    activated = None
    grant_error: Exception | None = None
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _REQUEST_MAX_AGE,
            )
            activated = await activate_coding_certification_inference_grant(
                session,
                grant_id=payload.grant_id,
                validator_hotkey=payload.validator_hotkey,
                broker_public_key=payload.broker_public_key,
                policy=transport.policy,
            )
        except ValidatorRequestReplayError:
            raise HTTPException(
                status_code=409,
                detail="coding certification lease request replayed",
                headers=_NO_STORE,
            ) from None
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantIntegrityError,
        ) as error:
            grant_error = error
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding certification inference grant is not live",
            headers=_NO_STORE,
        )
    if activated is None:
        raise HTTPException(
            status_code=503,
            detail="coding certification inference exchange result is unavailable",
            headers=_NO_STORE,
        )
    try:
        return CodingCertificationInferenceExchangeResponse.model_validate(
            {
                "schema": "dittobench-coding-certification-inference-exchange-v1",
                **_certification_grant_authority(activated.grant),
                "status": "active",
                "generation": activated.grant.generation,
                "bearer": activated.bearer,
                "proxy_url": transport.proxy_url,
                "revoke_bearer": activated.revoke_bearer,
                "revoke_url": transport.revoke_url,
            }
        )
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(
            status_code=503,
            detail="coding certification inference transport is invalid",
            headers=_NO_STORE,
        ) from None


@router.post(
    "/coding-certification-leases/inference-revoke",
    response_model=CodingCertificationInferenceRevokeResponse,
)
async def revoke_coding_certification_inference_grant_endpoint(
    payload: CodingInferenceRevokeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingCertificationInferenceRevokeResponse:
    """Durably revoke exactly the validator's observed canary grant generation."""

    response.headers["Cache-Control"] = "no-store"
    now = await _verify_signed_request(
        request=request,
        chain=chain,
        validator_hotkey=payload.validator_hotkey,
        requested_at=payload.requested_at,
        signature=payload.signature,
        message=coding_inference_revoke_signing_message(
            validator_hotkey=payload.validator_hotkey,
            grant_id=payload.grant_id,
            generation=payload.generation,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )
    revoked = None
    grant_error: Exception | None = None
    async with session.begin():
        try:
            await consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + _REQUEST_MAX_AGE,
            )
            revoked = await revoke_coding_certification_inference_grant(
                session,
                grant_id=payload.grant_id,
                validator_hotkey=payload.validator_hotkey,
                generation=payload.generation,
            )
        except ValidatorRequestReplayError:
            raise HTTPException(
                status_code=409,
                detail="coding certification lease request replayed",
                headers=_NO_STORE,
            ) from None
        except (
            CodingInferenceGrantNotAvailableError,
            CodingInferenceGrantIntegrityError,
            CodingInferenceGrantConflictError,
        ) as error:
            grant_error = error
    if isinstance(grant_error, CodingInferenceGrantNotAvailableError):
        raise HTTPException(
            status_code=404,
            detail="coding certification inference grant is unavailable",
            headers=_NO_STORE,
        )
    if grant_error is not None:
        raise HTTPException(
            status_code=409,
            detail="coding certification inference grant is not revocable",
            headers=_NO_STORE,
        )
    if revoked is None or revoked.grant.revoked_at is None:
        raise HTTPException(
            status_code=503,
            detail="coding certification inference revocation result is unavailable",
            headers=_NO_STORE,
        )
    return CodingCertificationInferenceRevokeResponse(
        schema="dittobench-coding-certification-inference-revocation-v1",
        coding_contract_version=1,
        weight_eligible=False,
        grant_id=revoked.grant.grant_id,
        lease_id=revoked.grant.lease_id,
        status="revoked",
        generation=revoked.grant.generation,
        revoked_at=revoked.grant.revoked_at,
        idempotent=revoked.idempotent,
    )


def _response(
    result: CodingCertificationLeaseResult,
) -> CodingCertificationLeaseResponse:
    if result.row.status == CodingCertificationLeaseStatus.EXPIRED.value:
        raise HTTPException(
            status_code=404,
            detail="coding certification lease is not available",
            headers=_NO_STORE,
        )
    return CodingCertificationLeaseResponse(
        authority=result.authority,
        status=CodingCertificationLeaseStatus(result.row.status),
        claimed_at=result.row.claimed_at,
        aborted_at=result.row.aborted_at,
        screened_image_id=result.row.screened_image_id,
        screened_image_ref=result.row.screened_image_ref,
        screened_image_upload_id=result.row.screened_image_upload_id,
        weight_eligible=False,
    )


async def _verify_signed_request(
    *,
    request: Request,
    chain: ChainClient,
    validator_hotkey: str,
    requested_at: datetime,
    signature: str,
    message: bytes,
) -> datetime:
    now = datetime.now(UTC)
    if abs(now - requested_at.astimezone(UTC)) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease request is stale",
            headers=_NO_STORE,
        )
    if not verify_signature(
        signer=validator_hotkey,
        payload=message,
        signature_hex=signature,
    ):
        raise ValidatorAuthError("coding certification lease signature did not verify")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    return now


async def _run_signed_lease_mutation(
    *,
    session: AsyncSession,
    validator_hotkey: str,
    nonce: UUID,
    now: datetime,
    mutate: Callable[[], Awaitable[CodingCertificationLeaseResult]],
    conflict_detail: str,
) -> CodingCertificationLeaseResult:
    try:
        async with session.begin():
            result = await mutate()
            try:
                await consume_validator_nonce(
                    session,
                    nonce=nonce,
                    validator_hotkey=validator_hotkey,
                    now=now,
                    expires_at=now + _REQUEST_MAX_AGE,
                )
            except ValidatorRequestReplayError:
                if not result.idempotent:
                    raise
            return result
    except ValidatorRequestReplayError as error:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease request replayed",
            headers=_NO_STORE,
        ) from error
    except (
        CodingCertificationLeaseNotAvailableError,
        CodingCertificationLeaseConflictError,
        CodingCertificationLeaseUnavailableError,
    ) as error:
        await _consume_failed_request_nonce(
            session=session,
            validator_hotkey=validator_hotkey,
            nonce=nonce,
            now=now,
        )
        if isinstance(error, CodingCertificationLeaseNotAvailableError):
            raise HTTPException(
                status_code=404,
                detail="coding certification lease is not available",
                headers=_NO_STORE,
            ) from None
        if isinstance(error, CodingCertificationLeaseConflictError):
            raise HTTPException(
                status_code=409,
                detail=conflict_detail,
                headers=_NO_STORE,
            ) from None
        raise HTTPException(
            status_code=503,
            detail="public certification canary is unavailable",
            headers=_NO_STORE,
        ) from None


async def _consume_failed_request_nonce(
    *,
    session: AsyncSession,
    validator_hotkey: str,
    nonce: UUID,
    now: datetime,
) -> None:
    try:
        async with session.begin():
            await consume_validator_nonce(
                session,
                nonce=nonce,
                validator_hotkey=validator_hotkey,
                now=now,
                expires_at=now + _REQUEST_MAX_AGE,
            )
    except ValidatorRequestReplayError as error:
        raise HTTPException(
            status_code=409,
            detail="coding certification lease request replayed",
            headers=_NO_STORE,
        ) from error
