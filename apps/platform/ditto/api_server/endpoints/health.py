"""``GET /health`` - DB + chain reachability probe, and deploy-revision truth.

Beyond liveness, this endpoint answers the question that went unasked during
the 2026-07-25 near-outage: *is the running process on the checked-out
revision?* ``commit`` is resolved at process start; ``checked_out_commit`` is
re-read from the deploy directory per probe. When they disagree, the host has
new code on disk that nothing is running -- see
:mod:`ditto.api_server.revision`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import HealthResponse
from ditto.api_server import revision
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.chain import ChainClient, ChainError

DepStatus = Literal["ok", "down"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ChainDep = Annotated[ChainClient, Depends(get_chain_client)]


@router.get(
    "/health",
    response_model=HealthResponse,
    include_in_schema=False,
)
async def health(
    request: Request,
    response: Response,
    session: SessionDep,
    chain: ChainDep,
) -> HealthResponse:
    """Probe DB + chain liveness. 503 when any dependency is down."""
    db_status: DepStatus = "ok"
    chain_status: DepStatus = "ok"

    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        # Plain string, no exc_info: Prometheus scrape rate would flood
        # logs with full tracebacks if a dep flickers.
        logger.warning(f"health probe: db unreachable: {e}")
        db_status = "down"

    try:
        await chain.get_latest_block()
    except ChainError as e:
        logger.warning(f"health probe: chain unreachable: {e}")
        chain_status = "down"

    overall: DepStatus = "ok" if db_status == "ok" and chain_status == "ok" else "down"
    if overall != "ok":
        response.status_code = 503

    # Revision drift is reported, never enforced: a host running yesterday's
    # code is a deploy problem, and 503-ing it here would take the instance out
    # of service over it. The deploy script is what fails on a mismatch.
    running = request.app.state.commit_hash
    checked_out = await revision.checked_out_commit()

    return HealthResponse(
        status=overall,
        db=db_status,
        chain=chain_status,
        commit=running,
        checked_out_commit=checked_out,
        commit_drift=revision.commits_diverged(running, checked_out),
    )
