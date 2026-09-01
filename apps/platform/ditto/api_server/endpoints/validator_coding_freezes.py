"""Validator intake for immutable shadow coding authoring freezes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evaluation import (
    SubmitCodingAuthoringFreezeRequest,
    SubmitCodingAuthoringFreezeResponse,
    coding_authoring_freeze_signing_message,
)
from ditto.api_models.coding_evidence_upload import CodingSealedEvidenceKind
from ditto.api_server.attestation import verify_signature
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.chain import ChainClient
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingShadowAuthoringFreeze,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries.coding_certifications import (
    coding_certification_settlement_bound,
)
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    coding_authoring_freeze_matches,
    insert_coding_authoring_freeze,
)
from ditto.db.queries.coding_evidence_uploads import (
    CodingSealedEvidenceConflictError,
    CodingSealedEvidenceExpectation,
    CodingSealedEvidenceIdentity,
    require_coding_sealed_evidence_finalizations,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _response(
    *,
    row: CodingShadowAuthoringFreeze,
    agent_id: UUID,
    coding_run_id: str,
    idempotent: bool,
) -> SubmitCodingAuthoringFreezeResponse:
    return SubmitCodingAuthoringFreezeResponse(
        freeze_id=row.freeze_id,
        agent_id=agent_id,
        run_row_id=row.run_row_id,
        ticket_id=row.ticket_id,
        coding_run_id=coding_run_id,
        authoring_evidence_sha256=row.authoring_evidence_sha256,
        frozen_at=row.created_at,
        accepted=True,
        idempotent=idempotent,
        weight_eligible=False,
    )


@router.post(
    "/coding-shadow/authoring-freeze",
    response_model=SubmitCodingAuthoringFreezeResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Agent or coding shadow lease not found."},
        409: {"description": "Authority, deadline, or immutable freeze conflict."},
    },
)
async def submit_coding_authoring_freeze(
    payload: SubmitCodingAuthoringFreezeRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> SubmitCodingAuthoringFreezeResponse:
    """Persist authoring evidence without releasing any grader capability."""

    response.headers["Cache-Control"] = "no-store"
    raw_body = await request.body()
    raw_body_sha256 = hashlib.sha256(raw_body).hexdigest()
    agent_id = payload.agent_id
    signed = coding_authoring_freeze_signing_message(
        validator_hotkey=payload.validator_hotkey,
        agent_id=agent_id,
        bench_version=payload.bench_version,
        run_row_id=payload.run_row_id,
        ticket_id=payload.ticket_id,
        ticket_deadline=payload.ticket_deadline,
        coding_run_id=payload.coding_run_id,
        agent_artifact_sha256=payload.agent_artifact_sha256,
        screened_image_sha256=payload.screened_image_sha256,
        run_manifest_sha256=payload.run_manifest_sha256,
        task_set_manifest_sha256=payload.task_set_manifest_sha256,
        authoring_evidence_sha256=payload.authoring_evidence_sha256,
        authoring_transcript_object_key=(payload.authoring_transcript_object_key),
        authoring_transcript_bytes=payload.authoring_transcript_bytes,
        authoring_event_count=payload.authoring_event_count,
        frozen_submission_object_key=payload.frozen_submission_object_key,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding authoring freeze signature did not verify")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )

    async with session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        run = await session.get(
            CodingShadowRun,
            payload.run_row_id,
            with_for_update=True,
        )
        ticket = await session.get(
            CodingShadowTicket,
            payload.ticket_id,
            with_for_update=True,
        )
        if (
            agent is None
            or run is None
            or ticket is None
            or ticket.run_row_id != run.run_row_id
            or run.agent_id != agent_id
            or ticket.validator_hotkey != payload.validator_hotkey
        ):
            raise HTTPException(
                status_code=404,
                detail="matching coding shadow run and ticket not found",
            )
        evidence = payload.evidence
        if (
            agent.sha256 != run.artifact_sha256
            or payload.agent_artifact_sha256 != run.artifact_sha256
            or agent.screened_image_sha256 != run.screened_image_sha256
            or payload.screened_image_sha256 != run.screened_image_sha256
            or payload.bench_version != run.bench_version
            or payload.coding_run_id != run.coding_run_id
            or payload.run_manifest_sha256 != run.run_manifest_sha256
            or payload.task_set_manifest_sha256 != run.task_set_manifest_sha256
            or evidence.model.inference_grant_sha256 != run.inference_grant_sha256
            or _aware(payload.ticket_deadline) != _aware(ticket.deadline)
            or run.coding_contract_version != 1
            or run.task_count != 1
            or ticket.task_count != 1
        ):
            raise HTTPException(
                status_code=409,
                detail="coding authoring freeze does not match lease authority",
            )

        existing = await session.scalar(
            select(CodingShadowAuthoringFreeze).where(
                CodingShadowAuthoringFreeze.ticket_id == ticket.ticket_id
            )
        )
        try:
            await require_coding_sealed_evidence_finalizations(
                session,
                ticket=ticket,
                expectations=(
                    CodingSealedEvidenceExpectation(
                        evidence_kind=(CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT),
                        identities=(
                            CodingSealedEvidenceIdentity(
                                sha256=evidence.authoring_transcript_sha256,
                                size_bytes=payload.authoring_transcript_bytes,
                            ),
                        ),
                    ),
                    CodingSealedEvidenceExpectation(
                        evidence_kind=CodingSealedEvidenceKind.FROZEN_SUBMISSION,
                        identities=(
                            CodingSealedEvidenceIdentity(
                                sha256=evidence.frozen_patch_sha256,
                                size_bytes=None,
                            ),
                        ),
                    ),
                    CodingSealedEvidenceExpectation(
                        evidence_kind=(
                            CodingSealedEvidenceKind.AUTHORING_PUBLICATION_REQUEST
                        ),
                        identities=(
                            CodingSealedEvidenceIdentity(
                                sha256=raw_body_sha256,
                                size_bytes=len(raw_body),
                            ),
                        ),
                    ),
                ),
            )
        except CodingSealedEvidenceConflictError as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
            ) from None
        if existing is not None:
            if not coding_authoring_freeze_matches(
                existing,
                evidence=evidence,
                authoring_evidence_sha256=payload.authoring_evidence_sha256,
                authoring_transcript_object_key=(
                    payload.authoring_transcript_object_key
                ),
                authoring_transcript_bytes=payload.authoring_transcript_bytes,
                authoring_event_count=payload.authoring_event_count,
                frozen_submission_object_key=payload.frozen_submission_object_key,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="coding ticket already names different authoring evidence",
                )
            return _response(
                row=existing,
                agent_id=agent_id,
                coding_run_id=run.coding_run_id,
                idempotent=True,
            )
        if await session.scalar(
            select(CodingShadowResult.result_id).where(
                CodingShadowResult.ticket_id == ticket.ticket_id
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="coding result already finalized before authoring freeze",
            )
        certification = await session.get(
            CodingCapabilityCertification,
            ticket.certification_row_id,
        )
        database_now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(database_now, datetime):  # pragma: no cover
            raise RuntimeError("database clock did not return a timestamp")
        if (
            _aware(ticket.deadline) <= _aware(database_now)
            or ticket.claim_instance_id is None
            or ticket.claim_started_at is None
            or ticket.claim_expires_at is None
            or _aware(ticket.claim_expires_at) <= _aware(database_now)
            or certification is None
            or certification.status != "certified"
            or not coding_certification_settlement_bound(certification)
            or certification.agent_id != agent_id
            or certification.validator_hotkey != payload.validator_hotkey
            or certification.artifact_sha256 != run.artifact_sha256
            or certification.screened_image_sha256 != run.screened_image_sha256
            or certification.bench_version != run.bench_version
            or certification.coding_contract_version != 1
            or _aware(certification.expires_at) <= _aware(ticket.deadline)
        ):
            raise HTTPException(
                status_code=409,
                detail="coding shadow ticket or capability certification expired",
            )
        try:
            inserted = await insert_coding_authoring_freeze(
                session,
                ticket=ticket,
                evidence=evidence,
                authoring_evidence_sha256=payload.authoring_evidence_sha256,
                authoring_transcript_object_key=(
                    payload.authoring_transcript_object_key
                ),
                authoring_transcript_bytes=payload.authoring_transcript_bytes,
                authoring_event_count=payload.authoring_event_count,
                frozen_submission_object_key=payload.frozen_submission_object_key,
                signature=payload.signature,
            )
        except CodingShadowConflictError:
            raise HTTPException(
                status_code=409,
                detail="coding authoring freeze conflicts with immutable authority",
            ) from None
        row = inserted.row
        if not isinstance(row, CodingShadowAuthoringFreeze):  # pragma: no cover
            raise RuntimeError("coding authoring freeze insert returned wrong row")

    return _response(
        row=row,
        agent_id=agent_id,
        coding_run_id=run.coding_run_id,
        idempotent=inserted.idempotent,
    )
