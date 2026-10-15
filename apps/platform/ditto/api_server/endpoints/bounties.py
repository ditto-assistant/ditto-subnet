"""Build-bounty publication (operators) and the public board (everyone)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.bounty import (
    BountyBoardPage,
    BountyDetail,
    BountyPublishPreview,
    BountyPublishPreviewRequest,
    BountyPublishRequest,
    BountyRevisionView,
)
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server import bounty
from ditto.api_server.contributor_identity import admin_actor
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.no_store import no_store

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ActorDep = Annotated[str, Depends(admin_actor)]
# Board state changes on operator and contributor actions; a short shared
# cache absorbs polling without hiding a change for long.
BOARD_CACHE_CONTROL = "public, max-age=15, stale-while-revalidate=60"

public_router = APIRouter(prefix="/public/bounties", tags=["bounties"])
admin_router = APIRouter(
    prefix="/admin/bounties", tags=["bounties"], dependencies=[Depends(no_store)]
)


@public_router.get("", response_model=BountyBoardPage)
async def board(
    response: Response,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> BountyBoardPage:
    page = await bounty.board(session, limit=limit, offset=offset)
    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    return page


@public_router.get(
    "/{bounty_id}",
    response_model=BountyDetail,
    responses={404: {"model": HttpErrorEnvelope, "description": "Unknown bounty."}},
)
async def bounty_detail(
    bounty_id: UUID, response: Response, session: SessionDep
) -> BountyDetail:
    view = await bounty.detail(session, bounty_id)
    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    return view


@admin_router.post(
    "/preview",
    response_model=BountyPublishPreview,
    responses={status: {"model": HttpErrorEnvelope} for status in (401, 503)},
)
async def preview(
    body: BountyPublishPreviewRequest, _admin: AdminDep, session: SessionDep
) -> BountyPublishPreview:
    """The digest, confirmation phrase and refusals for a revision; writes nothing."""
    return await bounty.preview_revision(session, body)


@admin_router.post(
    "/revisions",
    response_model=BountyRevisionView,
    responses={status: {"model": HttpErrorEnvelope} for status in (400, 401, 409, 503)},
)
async def publish(
    body: BountyPublishRequest, _admin: AdminDep, actor: ActorDep, session: SessionDep
) -> BountyRevisionView:
    return await bounty.publish_revision(session, body, actor=actor)
