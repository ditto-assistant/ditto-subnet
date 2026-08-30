"""Authenticated screened-harness launch delivery for shadow coding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_harness import (
    CodingHarnessLaunchRequest,
    CodingHarnessLaunchResponse,
    coding_harness_launch_signing_message,
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
from ditto.api_server.storage import S3StorageClient
from ditto.chain import ChainClient
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_VALIDATOR_CODING_HARNESS,
    record_artifact_fetch,
)
from ditto.db.queries.coding_task_leases import (
    CodingTaskLeaseNotAvailableError,
    authorize_coding_shadow_harness_delivery,
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


@router.post(
    "/coding-shadow/harness-launch",
    response_model=CodingHarnessLaunchResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Coding screened harness unavailable."},
        409: {"description": "Replay, expiry, or immutable authority conflict."},
    },
)
async def request_coding_harness_launch(
    payload: CodingHarnessLaunchRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    storage: StorageDep,
) -> CodingHarnessLaunchResponse:
    """Return one short-lived screened image capability for an open ticket."""

    response.headers["Cache-Control"] = "no-store"
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=coding_harness_launch_signing_message(
            validator_hotkey=payload.validator_hotkey,
            ticket_id=payload.ticket_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        ),
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding harness launch signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding harness launch request timestamp is stale",
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
                detail="coding harness launch nonce has already been used",
            ) from None
        try:
            authority = await authorize_coding_shadow_harness_delivery(
                session,
                ticket_id=payload.ticket_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingTaskLeaseNotAvailableError:
            raise HTTPException(
                status_code=404,
                detail="coding screened harness is unavailable",
            ) from None
    if authority is None:  # pragma: no cover - exhaustive transaction outcome
        raise HTTPException(
            status_code=404,
            detail="coding screened harness is unavailable",
        )
    issued_at = datetime.now(UTC)
    ttl_seconds = min(
        int(_ARTIFACT_URL_TTL.total_seconds()),
        int((authority.deadline - issued_at).total_seconds()),
    )
    if ttl_seconds < 1:
        raise HTTPException(status_code=409, detail="coding harness ticket expired")
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
            refreshed = await authorize_coding_shadow_harness_delivery(
                session,
                ticket_id=payload.ticket_id,
                validator_hotkey=payload.validator_hotkey,
            )
        except CodingTaskLeaseNotAvailableError:
            raise HTTPException(
                status_code=409,
                detail="coding harness authority changed after URL minting",
            ) from None
    if refreshed != authority:
        raise HTTPException(
            status_code=409,
            detail="coding harness authority changed after URL minting",
        )
    if datetime.now(UTC) >= expires_at:
        raise HTTPException(status_code=409, detail="coding harness URL expired")
    await record_artifact_fetch(
        session,
        agent_id=authority.agent_id,
        endpoint=ENDPOINT_VALIDATOR_CODING_HARNESS,
        requester_kind="validator",
        requester_id=payload.validator_hotkey,
        lease_id=authority.ticket_id,
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
        return CodingHarnessLaunchResponse(
            schema="dittobench-coding-harness-launch-v1",
            coding_contract_version=1,
            weight_eligible=False,
            agent_id=authority.agent_id,
            run_row_id=authority.run_row_id,
            ticket_id=authority.ticket_id,
            ticket_deadline=authority.deadline,
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
            detail="coding harness launch authority is inconsistent",
        ) from None


__all__ = ["router"]
