"""Read-only operator surface for the finalized-block confirmation seed anchors."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.confirmation_seed_anchors import (
    AdminConfirmationSeedAnchor,
    AdminConfirmationSeedAnchorList,
)
from ditto.api_server import crn as crn_mod
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.models import Agent
from ditto.db.queries.benchmark_rollout import active_bench_version
from ditto.db.queries.confirmation_seed_anchors import (
    list_confirmation_seed_anchors,
)

router = APIRouter(prefix="/admin/confirmation-seed-anchors", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("", response_model=AdminConfirmationSeedAnchorList)
async def list_admin_confirmation_seed_anchors(
    _admin: AdminDep,
    session: SessionDep,
    bench_version: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AdminConfirmationSeedAnchorList:
    """List every reign anchor of one version -- pinned and still waiting.

    Defaults to the active benchmark. The ledger serves only pinned anchors,
    so a reign in its finality wait is visible only here; a binding version
    with zero rows means no continual-retest claim has opened a reign yet.
    """
    version = (
        bench_version
        if bench_version is not None
        else await active_bench_version(session)
    )
    rows = await list_confirmation_seed_anchors(
        session, bench_version=version, limit=limit
    )
    agents_by_id: dict[object, Agent] = {}
    if rows:
        agents = await session.scalars(
            select(Agent).where(
                Agent.agent_id.in_([row.champion_agent_id for row in rows])
            )
        )
        agents_by_id = {agent.agent_id: agent for agent in agents.all()}
    items = [
        AdminConfirmationSeedAnchor(
            champion_agent_id=row.champion_agent_id,
            champion_name=(
                agents_by_id[row.champion_agent_id].name
                if row.champion_agent_id in agents_by_id
                else None
            ),
            champion_miner_hotkey=(
                agents_by_id[row.champion_agent_id].miner_hotkey
                if row.champion_agent_id in agents_by_id
                else None
            ),
            bench_version=row.bench_version,
            ready_block=row.ready_block,
            anchor_block=row.anchor_block,
            anchor_block_hash=row.anchor_block_hash,
            pinned=row.anchor_block_hash is not None,
            pinned_at=row.pinned_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    pinned_count = sum(1 for item in items if item.pinned)
    return AdminConfirmationSeedAnchorList(
        bench_version=version,
        binding_active=crn_mod.crn_block_binding_active(version),
        binding_floor_bench_version=crn_mod.CRN_BLOCK_BINDING_MIN_BENCH_VERSION,
        anchor_block_delta=crn_mod.CRN_ANCHOR_BLOCK_DELTA,
        items=items,
        count=len(items),
        pinned_count=pinned_count,
        waiting_count=len(items) - pinned_count,
    )
