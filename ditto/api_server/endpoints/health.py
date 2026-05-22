"""``GET /health`` - DB + chain reachability probe.

Returns HTTP 200 with per-dependency status when everything is reachable,
HTTP 503 with the same body shape when any dependency is down. Excluded
from the OpenAPI schema since it is ops infra rather than consumer API.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import HealthResponse
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
    except SQLAlchemyError:
        logger.warning("health probe: db unreachable", exc_info=True)
        db_status = "down"

    try:
        await chain.get_latest_block()
    except ChainError:
        logger.warning("health probe: chain unreachable", exc_info=True)
        chain_status = "down"

    overall: DepStatus = (
        "ok" if db_status == "ok" and chain_status == "ok" else "down"
    )
    if overall != "ok":
        response.status_code = 503

    return HealthResponse(
        status=overall,
        db=db_status,
        chain=chain_status,
        commit=request.app.state.commit_hash,
    )
