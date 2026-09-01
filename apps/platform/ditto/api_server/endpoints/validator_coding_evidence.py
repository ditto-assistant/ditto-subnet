"""Signed reservation and PUT-capability issuance for sealed coding evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceUploadCapability,
    CodingSealedEvidenceUploadCapabilityRequest,
    coding_sealed_evidence_upload_signing_message,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.coding_sealed_evidence_storage import (
    CodingSealedEvidenceStorageIntegrityError,
    CodingSealedEvidenceStorageUnavailableError,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.queries.coding_evidence_uploads import (
    CodingSealedEvidenceConflictError,
    CodingSealedEvidenceNotAvailableError,
    reserve_coding_sealed_evidence_upload,
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
    "/coding-shadow/evidence-upload-capability",
    response_model=CodingSealedEvidenceUploadCapability,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Live started coding claim not available."},
        409: {"description": "Replay or immutable evidence conflict."},
        503: {"description": "Dedicated evidence store is not configured."},
    },
)
async def request_coding_evidence_upload_capability(
    payload: CodingSealedEvidenceUploadCapabilityRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> CodingSealedEvidenceUploadCapability:
    """Reserve exact bytes and mint one claim-bounded PUT bearer capability."""

    response.headers["Cache-Control"] = "no-store"
    message = coding_sealed_evidence_upload_signing_message(
        validator_hotkey=payload.validator_hotkey,
        instance_id=payload.instance_id,
        ticket_id=payload.ticket_id,
        claim_generation=payload.claim_generation,
        evidence_kind=payload.evidence_kind,
        sha256=payload.sha256,
        size_bytes=payload.size_bytes,
        nonce=payload.nonce,
        requested_at=payload.requested_at,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=message,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding evidence upload signature did not verify")
    now = datetime.now(UTC)
    if abs(now - payload.requested_at.astimezone(UTC)) > _REQUEST_MAX_AGE:
        raise HTTPException(
            status_code=409,
            detail="coding evidence upload request is stale",
            headers=_NO_STORE,
        )
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    minter = getattr(
        request.app.state,
        "coding_sealed_evidence_capability_minter",
        None,
    )
    if minter is None:
        raise HTTPException(
            status_code=503,
            detail="coding sealed evidence storage is not configured",
            headers=_NO_STORE,
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
            reservation = await reserve_coding_sealed_evidence_upload(
                session,
                validator_hotkey=payload.validator_hotkey,
                instance_id=payload.instance_id,
                ticket_id=payload.ticket_id,
                claim_generation=payload.claim_generation,
                evidence_kind=payload.evidence_kind,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
            )
    except ValidatorRequestReplayError:
        raise HTTPException(
            status_code=409,
            detail="coding evidence upload nonce has already been used",
            headers=_NO_STORE,
        ) from None
    except CodingSealedEvidenceNotAvailableError:
        raise HTTPException(
            status_code=404,
            detail="coding evidence upload claim is unavailable",
            headers=_NO_STORE,
        ) from None
    except CodingSealedEvidenceConflictError:
        raise HTTPException(
            status_code=409,
            detail="coding evidence upload identity conflicts",
            headers=_NO_STORE,
        ) from None

    ticket = reservation.ticket
    if ticket.claim_expires_at is None:
        raise HTTPException(
            status_code=409,
            detail="coding evidence claim expiry is inconsistent",
            headers=_NO_STORE,
        )
    try:
        return await minter.mint(
            reservation.upload,
            ticket_deadline=ticket.deadline,
            claim_expires_at=ticket.claim_expires_at,
        )
    except CodingSealedEvidenceStorageUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="coding evidence signer is temporarily unavailable",
            headers=_NO_STORE,
        ) from None
    except CodingSealedEvidenceStorageIntegrityError:
        raise HTTPException(
            status_code=409,
            detail="coding evidence capability authority is inconsistent",
            headers=_NO_STORE,
        ) from None
