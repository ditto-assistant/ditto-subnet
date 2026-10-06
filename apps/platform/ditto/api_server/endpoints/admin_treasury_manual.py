"""Backroom-only manual confirmation. No private key in Platform."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_manual import ManualPreviewInput, ManualSubmitInput
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.treasury_manual import preview, state, submit

router = APIRouter(prefix="/admin/treasury-manual", tags=["admin"])
Admin = Annotated[None, Depends(require_admin)]
Session = Annotated[AsyncSession, Depends(get_session)]


def enabled(request):
    loop = getattr(request.app.state, "treasury_manual_loop", None)
    return loop is not None and loop.enabled


@router.get("")
async def get_manual(
    request: Request, response: Response, _admin: Admin, session: Session
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    result = await state(session, request.app.state.config, enabled=enabled(request))
    loop = getattr(request.app.state, "treasury_manual_loop", None)
    result["bridge_error"] = loop.last_error if loop else None
    if result["bridge_error"] and not result["blocked_reason"]:
        result["blocked_reason"] = result["bridge_error"]
    return result


@router.post("/preview")
async def preview_manual(
    payload: ManualPreviewInput,
    request: Request,
    response: Response,
    _admin: Admin,
    session: Session,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await preview(
            session, request.app.state.config, payload, enabled=enabled(request)
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.post("")
async def submit_manual(
    payload: ManualSubmitInput,
    request: Request,
    response: Response,
    _admin: Admin,
    session: Session,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    # This is a Backroom asserted staff actor, under the server admin bearer.
    # Browser body actor fields are ignored; Backroom supplies its live session.
    if not x_admin_actor or not 1 <= len(x_admin_actor) <= 120:
        raise HTTPException(422, "Backroom operator audit actor required")
    try:
        async with session.begin():
            return await submit(
                session,
                request.app.state.config,
                payload,
                x_admin_actor,
                enabled=enabled(request),
            )
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
