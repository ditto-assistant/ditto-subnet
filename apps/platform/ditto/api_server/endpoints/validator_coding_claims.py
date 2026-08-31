"""Signed exclusive worker claims for shadow coding tickets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_claims import (
    CodingClaimActionRequest,
    CodingClaimNextRequest,
    CodingClaimResponse,
    coding_claim_action_signing_message,
    coding_claim_next_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.queries.coding_claims import (
    CodingClaimConflictError,
    CodingClaimNotAvailableError,
    CodingTicketClaim,
    claim_next_coding_ticket,
    heartbeat_coding_ticket_claim,
    start_coding_ticket_claim,
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
    "/coding-shadow/claims/next",
    response_model=CodingClaimResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "No coding ticket is claimable."},
        409: {"description": "Replay or claim authority conflict."},
    },
)
async def claim_next(
    payload: CodingClaimNextRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingClaimResponse:
    response.headers["Cache-Control"] = "no-store"
    await _authenticate(
        request=request,
        chain=chain,
        session=session,
        payload=payload,
        message=coding_claim_next_signing_message(
            validator_hotkey=payload.validator_hotkey,
            instance_id=payload.instance_id,
            run_row_id=payload.run_row_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )
    try:
        async with session.begin():
            result = await claim_next_coding_ticket(
                session,
                validator_hotkey=payload.validator_hotkey,
                instance_id=payload.instance_id,
                run_row_id=payload.run_row_id,
            )
    except CodingClaimConflictError:
        raise HTTPException(
            status_code=409,
            detail="coding ticket claim conflicts",
            headers=_NO_STORE,
        ) from None
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="no coding ticket is claimable",
            headers=_NO_STORE,
        )
    return _response(result)


@router.post(
    "/coding-shadow/claims/{ticket_id}/start",
    response_model=CodingClaimResponse,
)
async def start_claim(
    ticket_id: UUID,
    payload: CodingClaimActionRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingClaimResponse:
    return await _claim_action(
        action="start",
        ticket_id=ticket_id,
        payload=payload,
        request=request,
        response=response,
        chain=chain,
        session=session,
    )


@router.post(
    "/coding-shadow/claims/{ticket_id}/heartbeat",
    response_model=CodingClaimResponse,
)
async def heartbeat_claim(
    ticket_id: UUID,
    payload: CodingClaimActionRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingClaimResponse:
    return await _claim_action(
        action="heartbeat",
        ticket_id=ticket_id,
        payload=payload,
        request=request,
        response=response,
        chain=chain,
        session=session,
    )


async def _claim_action(
    *,
    action: Literal["start", "heartbeat"],
    ticket_id: UUID,
    payload: CodingClaimActionRequest,
    request: Request,
    response: Response,
    chain: ChainClient,
    session: AsyncSession,
) -> CodingClaimResponse:
    response.headers["Cache-Control"] = "no-store"
    if payload.ticket_id != ticket_id:
        raise HTTPException(
            status_code=409,
            detail="coding claim ticket mismatch",
            headers=_NO_STORE,
        )
    await _authenticate(
        request=request,
        chain=chain,
        session=session,
        payload=payload,
        message=coding_claim_action_signing_message(
            action=action,
            validator_hotkey=payload.validator_hotkey,
            instance_id=payload.instance_id,
            run_row_id=payload.run_row_id,
            ticket_id=ticket_id,
            claim_generation=payload.claim_generation,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
    )
    try:
        async with session.begin():
            function = (
                start_coding_ticket_claim
                if action == "start"
                else heartbeat_coding_ticket_claim
            )
            result = await function(
                session,
                validator_hotkey=payload.validator_hotkey,
                instance_id=payload.instance_id,
                run_row_id=payload.run_row_id,
                ticket_id=ticket_id,
                claim_generation=payload.claim_generation,
            )
    except CodingClaimNotAvailableError:
        raise HTTPException(
            status_code=404,
            detail="coding ticket claim unavailable",
            headers=_NO_STORE,
        ) from None
    except CodingClaimConflictError:
        raise HTTPException(
            status_code=409,
            detail="coding ticket claim conflicts",
            headers=_NO_STORE,
        ) from None
    return _response(result)


async def _authenticate(
    *,
    request: Request,
    chain: ChainClient,
    session: AsyncSession,
    payload: CodingClaimNextRequest,
    message: bytes,
) -> None:
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding claim request is stale",
            headers=_NO_STORE,
        )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=message,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding claim signature did not verify")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    try:
        async with session.begin():
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
            detail="coding claim request nonce has already been used",
            headers=_NO_STORE,
        ) from None


def _response(result: CodingTicketClaim) -> CodingClaimResponse:
    ticket, run = result.ticket, result.run
    if ticket.claim_expires_at is None or ticket.claim_instance_id is None:
        raise HTTPException(
            status_code=409,
            detail="coding claim state is inconsistent",
            headers=_NO_STORE,
        )
    return CodingClaimResponse(
        schema="dittobench-coding-ticket-claim-v1",
        coding_contract_version=1,
        weight_eligible=False,
        validator_hotkey=ticket.validator_hotkey,
        instance_id=result.instance_id,
        claim_generation=ticket.claim_generation,
        claim_expires_at=ticket.claim_expires_at,
        claim_started_at=ticket.claim_started_at,
        idempotent=result.idempotent,
        agent_id=run.agent_id,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        ticket_deadline=ticket.deadline,
        bench_version=run.bench_version,
        coding_run_id=run.coding_run_id,
        agent_artifact_sha256=run.artifact_sha256,
        screened_image_sha256=run.screened_image_sha256,
        run_manifest_sha256=run.run_manifest_sha256,
        task_set_manifest_sha256=run.task_set_manifest_sha256,
    )


__all__ = ["router"]
