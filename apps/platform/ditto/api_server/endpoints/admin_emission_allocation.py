"""Audited control for the typed-weights emission allocation (SN-350).

One revision names burn, every paid track and every Gamma bucket in basis
points of total miner emission summing to 10000, and Platform derives the
burn share, track map and Gamma bucket allocation validators already fold.
The confirmation phrase spells every nonzero entry in canonical order, e.g.
``APPLY EMISSION ALLOCATION burn=5000 memory=3500 generator=1500``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.emission_allocation import (
    AdminEmissionAllocationRequest,
    EmissionAllocationControl,
    EmissionAllocationRevision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_allocation import (
    EmissionAllocationRefused,
    EmissionAllocationResolver,
    apply_allocation,
    emission_allocation_control,
    revision_from_row,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/emission-allocation", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


def _resolver(request: Request) -> EmissionAllocationResolver:
    resolver = getattr(request.app.state, "emission_allocation", None)
    if resolver is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="emission allocation unavailable")
    return resolver


def _netuid(request: Request) -> int | None:
    config = getattr(request.app.state, "config", None)
    chain = getattr(config, "chain", None)
    netuid = getattr(chain, "netuid", None)
    return netuid if isinstance(netuid, int) else None


def _invalidate(request: Request) -> None:
    for name in ("emission_allocation", "burn_settings", "track_share_settings"):
        resolver = getattr(request.app.state, name, None)
        if resolver is not None:
            resolver.invalidate()


@router.get("", response_model=EmissionAllocationControl)
async def get_emission_allocation(
    request: Request, _admin: AdminDep, session: SessionDep
) -> EmissionAllocationControl:
    return await emission_allocation_control(
        session, netuid=_netuid(request), ttl_seconds=_resolver(request).ttl_seconds
    )


@router.post("", response_model=EmissionAllocationRevision)
async def create_emission_allocation(
    request: Request,
    payload: AdminEmissionAllocationRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> EmissionAllocationRevision:
    _resolver(request)
    try:
        row = await apply_allocation(session, payload, netuid=_netuid(request))
        await session.commit()
    except EmissionAllocationRefused as error:
        await session.rollback()
        raise HTTPException(status_code=error.status, detail=str(error)) from error
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="emission allocation changed concurrently; refresh and retry",
        ) from error
    await session.refresh(row)
    _invalidate(request)
    return revision_from_row(row)
