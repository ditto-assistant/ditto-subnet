"""Backroom airdrop runs (SN-490): dry-run preview, guarded execute, reports.

Every route sits behind the admin bearer. ``require_admin`` appends the
``admin_activity`` audit row, with the Backroom operator from
``X-Admin-Actor``, before a POST runs. Recipients and amounts stay out of
that row's public projection. The run ledger keeps the actor, the reason
and every recipient's outcome.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.airdrop import (
    AirdropAsset,
    AirdropExecuteRequest,
    AirdropExecuteResponse,
    AirdropMode,
    AirdropPreview,
    AirdropPreviewRequest,
    AirdropRunList,
    AirdropRunReport,
)
from ditto.api_server import airdrop
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(prefix="/admin/airdrops", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[str | None, Header(alias="X-Admin-Actor", max_length=320)]


def _require_actor(actor: str | None) -> str:
    actor = (actor or "").strip()
    if not 1 <= len(actor) <= 320 or not actor.isprintable():
        raise HTTPException(422, "Backroom operator audit actor required")
    return actor


@router.get("", response_model=AirdropRunList)
async def list_airdrop_runs(
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    idempotency_key: Annotated[str | None, Query(max_length=128)] = None,
    asset: AirdropAsset | None = None,
    mode: AirdropMode | None = None,
) -> AirdropRunList:
    """Recent runs, newest first, plus the caps and executors in force."""
    response.headers["Cache-Control"] = "no-store"
    runtime = airdrop.runtime_from_state(request.app.state)
    return AirdropRunList(
        policy=airdrop.policy(runtime),
        runs=await airdrop.list_runs(
            session,
            limit=limit,
            offset=offset,
            idempotency_key=idempotency_key,
            asset=asset,
            mode=mode,
        ),
    )


@router.get("/{run_id}", response_model=AirdropRunReport)
async def get_airdrop_run(
    run_id: UUID, response: Response, _admin: AdminDep, session: SessionDep
) -> AirdropRunReport:
    """One run with every recipient's settlement result."""
    response.headers["Cache-Control"] = "no-store"
    report = await airdrop.run_report(session, run_id)
    if report is None:
        raise HTTPException(404, "airdrop run not found")
    return report


@router.post("/preview", response_model=AirdropPreview)
async def preview_airdrop(
    payload: AirdropPreviewRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> AirdropPreview:
    """Resolve, cap and price a run. Never writes the ledger or moves anything."""
    response.headers["Cache-Control"] = "no-store"
    runtime = airdrop.runtime_from_state(request.app.state)
    return await airdrop.build_preview(session, runtime, payload)


@router.post("/execute", response_model=AirdropExecuteResponse)
async def execute_airdrop(
    payload: AirdropExecuteRequest,
    request: Request,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    x_admin_actor: ActorDep = None,
) -> AirdropExecuteResponse:
    """Run an airdrop. ``dry_run`` defaults to true and then only previews.

    With ``dry_run=false`` the request must echo the preview's
    ``confirmation_digest`` and exact ``confirmation`` phrase. A retry with
    the same ``idempotency_key`` resumes the recorded run and never sends a
    recipient that has already been claimed.
    """
    response.headers["Cache-Control"] = "no-store"
    actor = _require_actor(x_admin_actor)
    runtime = airdrop.runtime_from_state(request.app.state)
    if payload.dry_run:
        return AirdropExecuteResponse(
            dry_run=True,
            preview=await airdrop.build_preview(session, runtime, payload),
            run=None,
        )
    # Release the request session; execute commits each step on its own.
    await session.close()
    preview, report = await airdrop.execute(
        request.app.state.session_maker, runtime, payload, actor
    )
    return AirdropExecuteResponse(dry_run=False, preview=preview, run=report)
