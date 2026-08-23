"""Validator intake for the separate shadow coding result ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_evaluation import (
    SubmitCodingShadowResultRequest,
    SubmitCodingShadowResultResponse,
    coding_shadow_result_signing_message,
)
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
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    coding_shadow_result_matches,
    insert_coding_shadow_result,
)

router = APIRouter(prefix="/validator", tags=["validator"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.post(
    "/agent/{agent_id}/coding-shadow-result",
    response_model=SubmitCodingShadowResultResponse,
    responses={
        401: {"description": "Signature invalid or validator not permitted."},
        404: {"description": "Agent or coding shadow lease not found."},
        409: {"description": "Authority, deadline, or immutable result conflict."},
    },
)
async def submit_coding_shadow_result(
    agent_id: UUID,
    payload: SubmitCodingShadowResultRequest,
    request: Request,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
) -> SubmitCodingShadowResultResponse:
    """Persist one signed repair result without touching ordinary scores."""

    response.headers["Cache-Control"] = "no-store"
    signed = coding_shadow_result_signing_message(
        validator_hotkey=payload.validator_hotkey,
        agent_id=agent_id,
        run_row_id=payload.run_row_id,
        ticket_id=payload.ticket_id,
        bench_version=payload.bench_version,
        ticket_deadline=payload.ticket_deadline,
        agent_artifact_sha256=payload.agent_artifact_sha256,
        screened_image_sha256=payload.screened_image_sha256,
        run_evidence_sha256=payload.run_evidence_sha256,
    )
    if not verify_signature(
        signer=payload.validator_hotkey,
        payload=signed,
        signature_hex=payload.signature,
    ):
        raise ValidatorAuthError("coding shadow result signature did not verify")
    await _assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )

    now = datetime.now(UTC)
    async with session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        run = await session.get(
            CodingShadowRun, payload.run_row_id, with_for_update=True
        )
        ticket = await session.get(
            CodingShadowTicket,
            payload.ticket_id,
            with_for_update=True,
        )
        if (
            run is None
            or ticket is None
            or ticket.run_row_id != run.run_row_id
            or run.agent_id != agent_id
            or ticket.validator_hotkey != payload.validator_hotkey
        ):
            raise HTTPException(
                status_code=404,
                detail="matching coding shadow run and ticket not found",
            )
        if (
            agent.sha256 != run.artifact_sha256
            or payload.agent_artifact_sha256 != run.artifact_sha256
            or agent.screened_image_sha256 != run.screened_image_sha256
            or payload.screened_image_sha256 != run.screened_image_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="coding shadow result targets a stale screened artifact",
            )
        evidence = payload.evidence
        if (
            evidence.coding_run_id != run.coding_run_id
            or evidence.validator_ticket_id != str(ticket.ticket_id)
            or evidence.run_manifest_sha256 != run.run_manifest_sha256
            or evidence.task_set_manifest_sha256 != run.task_set_manifest_sha256
            or evidence.coding_contract_version != run.coding_contract_version
            or payload.bench_version != run.bench_version
            or len(evidence.tasks) != run.task_count
            or _aware(payload.ticket_deadline) != _aware(ticket.deadline)
        ):
            raise HTTPException(
                status_code=409,
                detail="coding result evidence does not match lease authority",
            )

        existing = await session.scalar(
            select(CodingShadowResult).where(
                CodingShadowResult.ticket_id == ticket.ticket_id
            )
        )
        if existing is not None:
            if not coding_shadow_result_matches(
                existing,
                evidence=evidence,
                run_evidence_sha256=payload.run_evidence_sha256,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="coding ticket already names different result evidence",
                )
            return SubmitCodingShadowResultResponse(
                agent_id=agent_id,
                run_row_id=run.run_row_id,
                ticket_id=ticket.ticket_id,
                coding_run_id=run.coding_run_id,
                accepted=True,
                idempotent=True,
                weight_eligible=False,
            )
        certification = await session.get(
            CodingCapabilityCertification,
            ticket.certification_row_id,
        )
        if (
            _aware(ticket.deadline) <= now
            or ticket.claim_instance_id is None
            or ticket.claim_started_at is None
            or ticket.claim_expires_at is None
            or _aware(ticket.claim_expires_at) <= now
            or certification is None
            or certification.status != "certified"
            or certification.agent_id != agent_id
            or certification.validator_hotkey != payload.validator_hotkey
            or certification.artifact_sha256 != run.artifact_sha256
            or certification.screened_image_sha256 != run.screened_image_sha256
            or certification.coding_contract_version != run.coding_contract_version
            or _aware(certification.expires_at) <= now
        ):
            raise HTTPException(
                status_code=409,
                detail="coding shadow ticket or capability certification expired",
            )
        try:
            result = await insert_coding_shadow_result(
                session,
                ticket=ticket,
                evidence=evidence,
                run_evidence_sha256=payload.run_evidence_sha256,
                signature=payload.signature,
            )
        except CodingShadowConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return SubmitCodingShadowResultResponse(
        agent_id=agent_id,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        coding_run_id=run.coding_run_id,
        accepted=True,
        idempotent=result.idempotent,
        weight_eligible=False,
    )
