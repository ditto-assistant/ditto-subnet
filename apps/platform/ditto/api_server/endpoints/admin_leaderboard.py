"""Authenticated leaderboard projection that keeps stored agent names.

Public ``GET /public/leaderboard`` strikes colliding handles after an upheld
name claim. Operators still need the stored ``agents.name`` for audit, so
Backroom reads this route instead of the cacheable public board.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ditto.api_models.public import PublicLeaderboardResponse
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import (
    _LEADERBOARD_DETAIL_FIELDS,
    build_public_leaderboard,
)
from ditto.api_server.endpoints.validator import SessionDep

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/leaderboard",
    response_model=PublicLeaderboardResponse,
    response_model_exclude={"entries": {"__all__": _LEADERBOARD_DETAIL_FIELDS}},
)
async def admin_leaderboard(
    request: Request,
    response: Response,
    session: SessionDep,
    _admin: AdminDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
) -> PublicLeaderboardResponse:
    """Same ledger as the public board, with stored agent names."""
    board = await build_public_leaderboard(
        request,
        response,
        session,
        bench_version,
        strike_colliding_names=False,
    )
    response.headers["Cache-Control"] = "no-store"
    return board
