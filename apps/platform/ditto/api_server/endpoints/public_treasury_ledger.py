"""Public and admin reads of the per-epoch Gamma collector ledger."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.treasury_ledger import PublicTreasuryLedger
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.treasury_ledger import ledger_view, sampler_status

router = APIRouter(prefix="/public/treasury-ledger", tags=["public"])
admin_router = APIRouter(prefix="/admin/treasury-ledger", tags=["admin"])

# 480 epochs is about 24 days at SN118's 72-minute tempo.
Limit = Annotated[int, Query(ge=1, le=480)]


@router.get("", response_model=PublicTreasuryLedger)
async def get_public_treasury_ledger(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Limit = 72,
) -> PublicTreasuryLedger:
    response.headers["Cache-Control"] = "public, max-age=30"
    return await ledger_view(
        session,
        netuid=request.app.state.config.chain.netuid,
        limit=limit,
        sampler=sampler_status(request.app.state),
    )


@admin_router.get("", response_model=PublicTreasuryLedger)
async def get_admin_treasury_ledger(
    _admin: Annotated[None, Depends(require_admin)],
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Limit = 72,
) -> PublicTreasuryLedger:
    return await ledger_view(
        session,
        netuid=request.app.state.config.chain.netuid,
        limit=limit,
        sampler=sampler_status(request.app.state),
    )
