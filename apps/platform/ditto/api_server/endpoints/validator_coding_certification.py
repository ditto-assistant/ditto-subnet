"""Validator persistence for shadow-only coding capability certifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import (
    CodingCertificationStatus,
    SubmitCodingCertificationRequest,
    SubmitCodingCertificationResponse,
    coding_certification_signing_message,
)
from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseStatus,
)
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.models import Agent, CodingCertificationLease
from ditto.db.queries.coding_certifications import (
    CodingCertificationConflictError,
    CodingCertificationSettlementError,
    coding_certification_lease_accepts_receipt,
    coding_certification_matches,
    get_coding_certification_by_lease,
    get_coding_certification_identity,
    insert_coding_certification,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]

_MAX_ISSUED_AT_SKEW = timedelta(minutes=5)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.post(
    "/agent/{agent_id}/coding-certification",
    response_model=SubmitCodingCertificationResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Agent not found."},
        409: {"description": "Artifact, lease, receipt, or replay conflict."},
    },
)
async def submit_coding_certification(
    agent_id: UUID,
    payload: SubmitCodingCertificationRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> SubmitCodingCertificationResponse:
    """Append one signed shadow receipt without touching score state."""

    response.headers["Cache-Control"] = "no-store"
    receipt = payload.receipt
    signed = coding_certification_signing_message(
        validator_hotkey=payload.validator_hotkey,
        agent_id=agent_id,
        bench_version=payload.bench_version,
        lease_id=payload.lease_id,
        screened_image_sha256=payload.screened_image_sha256,
        certification_sha256=receipt.certification_sha256,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding certification signature did not verify")

    netuid = request.app.state.config.chain.netuid
    network = request.app.state.config.chain.subtensor_network
    await _assert_validator_permitted(
        chain, netuid, payload.validator_hotkey, network=network
    )

    now = datetime.now(UTC)
    try:
        issued_at = datetime.fromtimestamp(receipt.issued_at_unix, UTC)
        expires_at = datetime.fromtimestamp(receipt.expires_at_unix, UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise HTTPException(
            status_code=409,
            detail=(
                "coding certification receipt timestamps are outside supported bounds"
            ),
        ) from error
    async with session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if receipt.agent_artifact_sha256 != agent.sha256:
            raise HTTPException(
                status_code=409,
                detail="coding receipt artifact does not match the agent",
            )
        if (
            agent.screened_image_sha256 is None
            or payload.screened_image_sha256 != agent.screened_image_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="coding receipt screened image is absent or stale",
            )

        existing = await get_coding_certification_identity(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            coding_contract_version=receipt.coding_contract_version,
            certification_id=receipt.certification_id,
        )
        if existing is not None:
            if not coding_certification_matches(
                existing,
                artifact_sha256=agent.sha256,
                screened_image_sha256=payload.screened_image_sha256,
                bench_version=payload.bench_version,
                lease_id=payload.lease_id,
                ticket_deadline=_aware(existing.ticket_deadline),
                receipt=receipt,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="coding certification identity names different evidence",
                )
            return SubmitCodingCertificationResponse(
                agent_id=agent_id,
                certification_id=receipt.certification_id,
                status=receipt.status,
                accepted=True,
                idempotent=True,
                active=(
                    receipt.status is CodingCertificationStatus.CERTIFIED
                    and _aware(existing.expires_at) > now
                ),
            )

        if issued_at > now + _MAX_ISSUED_AT_SKEW or expires_at <= now:
            raise HTTPException(
                status_code=409,
                detail="coding certification receipt is not currently active",
            )
        by_lease = await get_coding_certification_by_lease(
            session, lease_id=payload.lease_id
        )
        if by_lease is not None:
            raise HTTPException(
                status_code=409,
                detail="coding certification identity names different evidence",
            )
        lease = await session.get(
            CodingCertificationLease, payload.lease_id, with_for_update=True
        )
        if lease is None or lease.validator_hotkey != payload.validator_hotkey:
            raise HTTPException(
                status_code=404, detail="coding certification lease is not available"
            )
        if (
            lease.status == CodingCertificationLeaseStatus.ISSUED.value
            and _aware(lease.deadline) <= now
        ):
            lease.status = CodingCertificationLeaseStatus.EXPIRED.value
            await session.flush()
            raise HTTPException(
                status_code=404, detail="coding certification lease is not available"
            )
        if not coding_certification_lease_accepts_receipt(
            lease,
            validator_hotkey=payload.validator_hotkey,
            agent_id=agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=payload.screened_image_sha256,
            bench_version=payload.bench_version,
            receipt=receipt,
        ):
            raise HTTPException(
                status_code=409,
                detail="no matching claimed certification lease",
            )
        lease_issued_at = _aware(lease.issued_at)
        lease_deadline = _aware(lease.deadline)
        if (
            issued_at < lease_issued_at - _MAX_ISSUED_AT_SKEW
            or issued_at > lease_deadline
        ):
            raise HTTPException(
                status_code=409,
                detail="coding certification receipt predates or postdates its lease",
            )
        try:
            result = await insert_coding_certification(
                session,
                agent_id=agent_id,
                artifact_sha256=agent.sha256,
                screened_image_sha256=payload.screened_image_sha256,
                validator_hotkey=payload.validator_hotkey,
                bench_version=payload.bench_version,
                lease_id=payload.lease_id,
                ticket_deadline=lease_deadline,
                receipt=receipt,
                signature=payload.signature,
            )
        except (
            CodingCertificationConflictError,
            CodingCertificationSettlementError,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return SubmitCodingCertificationResponse(
        agent_id=agent_id,
        certification_id=receipt.certification_id,
        status=receipt.status,
        accepted=True,
        idempotent=result.idempotent,
        active=(
            receipt.status is CodingCertificationStatus.CERTIFIED
            and _aware(result.row.expires_at) > now
        ),
    )
