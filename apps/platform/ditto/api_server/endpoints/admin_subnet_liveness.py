"""Backroom read of subnet liveness signals (ditto-subnet#2600, invariant 1).

Read-only by design: the computation in :mod:`ditto.api_server.subnet_liveness`
runs in one ``READ ONLY`` transaction with a statement timeout. It pages
nobody and gates nothing; alert delivery is an explicit #2600 follow-up that
should consume this read rather than re-derive it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.subnet_liveness import SubnetLiveness
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.subnet_liveness import load_subnet_liveness

router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("/admin/subnet-liveness", response_model=SubnetLiveness)
async def get_subnet_liveness(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    # Whole-deployment read: queues, scores, holds and the legacy GCP route are
    # not partitioned by environment, so only prod is honest to label.
    environment: Annotated[Literal["prod"], Query()] = "prod",
) -> SubnetLiveness:
    """Screening admission, scoring throughput, pin, hold, lease and collector."""
    return await load_subnet_liveness(
        session,
        environment=environment,
        netuid=request.app.state.config.chain.netuid,
        now=datetime.now(UTC),
    )
