"""Read-only retrieval endpoints.

Public, unauthed reads. Status + hotkey are chain-public-equivalent;
rate-limit + TLS deferred to a reverse proxy in front of the API
(threat-model G6 known gap). ``Cache-Control: no-store`` on every
response because these are state-machine status queries: polling
exists exactly to detect transitions, and any intermediate cache
defeats that.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import AgentResponse, AgentStatusResponse
from ditto.api_models.upload import _SS58_PATTERN
from ditto.api_server.dependencies import get_session
from ditto.db.queries.agents import (
    get_agent_by_id,
    get_latest_agent_by_hotkey,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retrieval", tags=["retrieval"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class AgentNotFoundError(Exception):
    """Raised when ``/retrieval/agent/{agent_id}/status`` does not find a row.

    This can happen when:
    - The caller provided a well-formed UUID that does not exist in
      ``agents`` (mistyped id, id from a different deployment).
    - The row was deleted by an admin action between the caller's prior
      upload response and this lookup (rare; not a normal flow).
    """


class HotkeyAgentNotFoundError(Exception):
    """Raised when ``/retrieval/agent-by-hotkey`` finds no agent for the hotkey.

    This can happen when:
    - The hotkey has never submitted an upload (most common; fresh miner).
    - All prior submissions from the hotkey were deleted by admin action.
    """


@router.get(
    "/agent-by-hotkey",
    response_model=AgentResponse,
    responses={
        404: {"description": "No agent for the given hotkey."},
        422: {"description": "Malformed hotkey query parameter."},
    },
)
async def agent_by_hotkey(
    miner_hotkey: Annotated[str, Query(pattern=_SS58_PATTERN)],
    session: SessionDep,
    response: Response,
) -> AgentResponse:
    """Return the most recent agent for ``miner_hotkey``, or 404."""
    response.headers["Cache-Control"] = "no-store"
    agent = await get_latest_agent_by_hotkey(session, miner_hotkey=miner_hotkey)
    if agent is None:
        raise HotkeyAgentNotFoundError(
            f"no agent found for miner_hotkey={miner_hotkey}"
        )
    return AgentResponse(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        name=agent.name,
        status=agent.status,
        sha256=agent.sha256,
        created_at=agent.created_at,
    )


@router.get(
    "/agent/{agent_id}/status",
    response_model=AgentStatusResponse,
    responses={
        404: {"description": "No agent with the given id."},
        422: {"description": "Malformed UUID path parameter."},
    },
)
async def agent_status(
    agent_id: UUID,
    session: SessionDep,
    response: Response,
) -> AgentStatusResponse:
    """Return the minimal lifecycle status for ``agent_id``, or 404."""
    response.headers["Cache-Control"] = "no-store"
    agent = await get_agent_by_id(session, agent_id=agent_id)
    if agent is None:
        raise AgentNotFoundError(f"no agent with id={agent_id}")
    return AgentStatusResponse(agent_id=agent.agent_id, status=agent.status)
