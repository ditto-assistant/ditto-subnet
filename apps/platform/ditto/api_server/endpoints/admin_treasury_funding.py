"""Private authenticated accounting observations; no fund-moving endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_funding import FundingBatch, FundingSubmit
from ditto.api_server import treasury_funding
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin/treasury-funding", tags=["admin"])
Admin = Annotated[None, Depends(require_admin)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("")
async def get_history(
    response: Response, _admin: Admin, session: Session
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await treasury_funding.history(session)


@router.post("/preview")
async def preview_history(
    batch: FundingBatch, response: Response, _admin: Admin, session: Session
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await treasury_funding.preview(session, batch)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("")
async def record_history(
    payload: FundingSubmit,
    response: Response,
    _admin: Admin,
    session: Session,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    actor = (x_admin_actor or "").strip()
    if not actor or len(actor) > 254 or not actor.isprintable():
        raise HTTPException(422, "Backroom operator audit actor required")
    try:
        async with session.begin():
            return await treasury_funding.record_batch(session, payload, actor)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
