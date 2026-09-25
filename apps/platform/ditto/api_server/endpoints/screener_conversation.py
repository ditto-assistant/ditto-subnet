"""Enrolled-worker access to the isolated shadow conversation lane."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from ditto.api_models.conversation import (
    ConversationObservation,
    ConversationResultRequest,
)
from ditto.api_server.dependencies import get_storage_client
from ditto.api_server.endpoints.admin_conversation import (
    SessionDep,
    claim_assessment,
    submit_result,
)
from ditto.api_server.endpoints.screener import ScreenerDep, _screened_image_key
from ditto.db.models import Agent, ConversationAssessment
from ditto_screening_protocol.conversation import ConversationLaunch

router = APIRouter(prefix="/screener/conversation-assessments", tags=["screener"])


@router.post("/claim", response_model=ConversationLaunch | None)
async def claim(
    request: Request, worker: ScreenerDep, session: SessionDep
) -> ConversationLaunch | None:
    if request.state.screener_node_status != "active":
        return None
    claim = await claim_assessment(request, session, worker)
    if claim is None:
        return None
    agent = await session.get(Agent, claim.agent_id)
    if agent is None or agent.screened_image_upload_id is None:
        raise HTTPException(409, "conversation image unavailable")
    storage = await get_storage_client(request)
    url = await storage.presigned_get_url(
        key=_screened_image_key(agent.agent_id, agent.screened_image_upload_id),
        expires_in=900,
    )
    return ConversationLaunch(
        **claim.model_dump(),
        screened_image_url=url,
        screened_image_id=agent.screened_image_id,
        screened_image_size_bytes=agent.screened_image_size_bytes,
    )


@router.post("/{assessment_id}/result", response_model=ConversationObservation)
async def result(
    assessment_id: UUID,
    payload: ConversationResultRequest,
    worker: ScreenerDep,
    session: SessionDep,
) -> ConversationObservation:
    row = await session.get(ConversationAssessment, assessment_id)
    if row is None or row.worker_hotkey != worker:
        raise HTTPException(403, "conversation claim belongs to another worker")
    usage = payload.report.harness_usage
    if payload.report.status == "completed" and (
        usage is None or usage.unmetered or usage.failed
    ):
        raise HTTPException(
            422, "completed conversation requires verified harness usage"
        )
    return await submit_result(assessment_id, payload, None, session)
