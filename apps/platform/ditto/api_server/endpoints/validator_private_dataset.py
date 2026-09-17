"""Private grading bytes require proof of possession and an exact live lease."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from ditto.api_models.private_dataset import PrivateDatasetRequest
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.endpoints import validator as v
from ditto.db.models import PrivateBenchmarkDataset, ValidatorTicket
from ditto.db.queries.private_benchmark_datasets import (
    PrivateDatasetIdentity,
    find_private_dataset,
)

router = APIRouter(prefix="/validator", tags=["validator"])


@router.post("/agent/{agent_id}/private-dataset", response_class=Response)
async def download_private_dataset(
    agent_id: UUID,
    payload: PrivateDatasetRequest,
    request: Request,
    session: v.SessionDep,
    chain: v.ChainDep,
) -> Response:
    now = datetime.now(UTC)
    if not v._verify_signature(
        payload.validator_hotkey, payload.signing_message(agent_id), payload.signature
    ):
        raise v.ValidatorAuthError("private dataset signature did not verify")
    if abs(now - payload.requested_at.astimezone(UTC)) > v._JOB_REQUEST_MAX_AGE:
        raise HTTPException(409, "private dataset request is stale")
    await v._assert_validator_permitted(
        chain,
        request.app.state.config.chain.netuid,
        payload.validator_hotkey,
        network=request.app.state.config.chain.subtensor_network,
    )
    async with session.begin():
        try:
            await v.consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=now + v._JOB_REQUEST_MAX_AGE,
            )
        except v.ValidatorRequestReplayError:
            raise HTTPException(409, "private dataset nonce already used") from None
        ticket = await session.scalar(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.validator_hotkey == payload.validator_hotkey,
                ValidatorTicket.bench_version == 13,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline == payload.deadline,
                ValidatorTicket.deadline > now,
                ValidatorTicket.dataset_sha256 == payload.dataset_sha256,
            )
            .with_for_update()
        )
        if ticket is None:
            raise HTTPException(409, "no matching live private dataset lease")
        row = await session.scalar(
            select(PrivateBenchmarkDataset)
            .where(
                PrivateBenchmarkDataset.dataset_sha256 == payload.dataset_sha256,
                PrivateBenchmarkDataset.seed == ticket.seed,
            )
            .limit(1)
        )
        if row is None:
            raise HTTPException(409, "private dataset is unavailable")
        artifact = await find_private_dataset(
            session,
            identity=PrivateDatasetIdentity(
                scope=row.scope,
                seed=row.seed,
                run_size=row.run_size,
                transform_profile_sha256=row.transform_profile_sha256,
                bench_version=row.bench_version,
            ),
        )
        if artifact is None:
            raise HTTPException(409, "private dataset is unavailable")
        body = artifact.dataset_bytes
    return Response(
        body,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Dataset-SHA256": payload.dataset_sha256,
        },
    )
