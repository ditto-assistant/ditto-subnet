"""Validator issue, claim, and abort for shadow coding-certification leases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAbortRequest,
    CodingCertificationLeaseClaimRequest,
    CodingCertificationLeaseIssueRequest,
    CodingCertificationLeaseResponse,
    CodingCertificationLeaseStatus,
    coding_certification_lease_abort_signing_message,
    coding_certification_lease_claim_signing_message,
    coding_certification_lease_issue_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    CodingCertificationLeaseResult,
    CodingCertificationLeaseUnavailableError,
    abort_coding_certification_lease,
    claim_coding_certification_lease,
    issue_coding_certification_lease,
)
from ditto.db.queries.validator_auth import (
    ValidatorRequestReplayError,
    consume_validator_nonce,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]
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
