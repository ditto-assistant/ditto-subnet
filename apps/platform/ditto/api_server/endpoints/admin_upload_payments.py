"""Admin: what Platform recorded about one upload-payment proof.

A miner who reports 3201, 3207 or 3208 quotes a finalized transfer. Whether an
upload consumed it, a credit still holds it, or Platform never recorded it
lived only in ``evaluation_payments`` and ``generator_submission_payments``, so
answering took a production database read (SN-455).

Strictly read-only. Platform records only: an unrecorded proof is not verified
on chain here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.upload import _BLOCK_HASH_PATTERN
from ditto.api_models.upload_payment_proof import (
    UploadPaymentProofAgent,
    UploadPaymentProofView,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent
from ditto.db.queries.payments import (
    get_evaluation_payment_for_proof,
    get_generator_payment_for_proof,
)
from ditto.db.queries.submission_settings import UPLOAD_ADMISSION_TTL

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/upload-payments/{block_hash}/{extrinsic_index}",
    response_model=UploadPaymentProofView,
)
async def get_upload_payment_proof(
    block_hash: Annotated[str, Path(pattern=_BLOCK_HASH_PATTERN)],
    extrinsic_index: Annotated[int, Path(ge=0, le=2**31 - 1)],
    _admin: AdminDep,
    session: SessionDep,
) -> UploadPaymentProofView:
    """Return the upload a proof funded, the credit holding it, or neither."""
    generated_at = datetime.now(UTC)
    payment = await get_evaluation_payment_for_proof(
        session, block_hash=block_hash, extrinsic_index=extrinsic_index
    )
    if payment is not None:
        funded = payment.agent_id is not None
        agent = await session.get(
            Agent, payment.agent_id if funded else payment.credit_for_agent_id
        )
        # The same window the upload paths enforce on a credit.
        expires_at = payment.timestamp + UPLOAD_ADMISSION_TTL
        return UploadPaymentProofView(
            block_hash=payment.block_hash,
            extrinsic_index=payment.extrinsic_index,
            state="funded_agent" if funded else "reusable_credit",
            miner_hotkey=payment.miner_hotkey,
            miner_coldkey=payment.miner_coldkey,
            amount_rao=payment.amount_rao,
            dest_address=payment.dest_address,
            block_timestamp=payment.timestamp,
            recorded_at=payment.created_at,
            agent=_agent_view(agent) if agent is not None else None,
            recovery_expires_at=None if funded else expires_at,
            recovery_expired=None if funded else expires_at <= generated_at,
            generated_at=generated_at,
        )

    generator_payment = await get_generator_payment_for_proof(
        session, block_hash=block_hash, extrinsic_index=extrinsic_index
    )
    if generator_payment is not None:
        return UploadPaymentProofView(
            block_hash=generator_payment.block_hash,
            extrinsic_index=generator_payment.extrinsic_index,
            state="funded_generator",
            miner_hotkey=generator_payment.miner_hotkey,
            miner_coldkey=generator_payment.miner_coldkey,
            amount_rao=generator_payment.amount_rao,
            dest_address=generator_payment.dest_address,
            block_timestamp=generator_payment.timestamp,
            recorded_at=generator_payment.created_at,
            generator_submission_id=generator_payment.submission_id,
            generated_at=generated_at,
        )

    return UploadPaymentProofView(
        block_hash=block_hash.lower(),
        extrinsic_index=extrinsic_index,
        state="unrecorded",
        generated_at=generated_at,
    )


def _agent_view(agent: Agent) -> UploadPaymentProofAgent:
    return UploadPaymentProofAgent(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        agent_version=agent.version,
        agent_status=agent.status.value,
        artifact_sha256=agent.sha256,
        submitted_at=agent.created_at,
    )
