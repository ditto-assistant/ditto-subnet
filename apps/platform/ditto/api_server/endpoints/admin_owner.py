"""Admin: resolve one key to the miner footprint its payment records imply.

Reviewers repeatedly need "who else does this operator control?" — adjudicating
whether a duplicate artifact came from the same miner or a third party, or
clustering a set of hotkeys that appeared in one leak. Until now the only
coldkey in the product was internal to the same-owner suppression logic, so the
question required a manual database query.

This endpoint answers it from ``evaluation_payments``: who paid for each
evaluation. That is **not** on-chain metagraph ownership, and the two can
disagree. Read the caveat carried on the response, and
:mod:`ditto.db.queries.ownership`, before treating a shared coldkey as a finding.

Strictly read-only. Nothing here is consulted by screening, scoring, weights, or
emissions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_owner import (
    AdminOwnerAgent,
    AdminOwnerFootprint,
    AdminOwnerHotkey,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.db.queries.ownership import MAX_DEPTH, resolve_owner_footprint

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]


@router.get("/miner-owners/{identifier}", response_model=AdminOwnerFootprint)
async def get_miner_owner_footprint(
    identifier: str,
    _admin: AdminDep,
    session: SessionDep,
    depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 1,
    agents_per_hotkey: Annotated[int, Query(ge=0, le=50)] = 10,
) -> AdminOwnerFootprint:
    """Return every hotkey linked to ``identifier`` through payment records.

    ``identifier`` may be a miner hotkey or a payment coldkey — hotkeys and
    coldkeys are both SS58 addresses, so the records decide which, and the
    answer says which role it found.
    """
    footprint = await resolve_owner_footprint(
        session,
        identifier=identifier,
        depth=depth,
        agents_per_hotkey=agents_per_hotkey,
    )
    return AdminOwnerFootprint(
        identifier=footprint.identifier,
        identifier_kind=footprint.identifier_kind,
        depth=footprint.depth,
        miner_coldkeys=list(footprint.miner_coldkeys),
        hotkey_count=footprint.hotkey_count,
        submission_count=footprint.submission_count,
        expansion_complete=footprint.expansion_complete,
        hotkeys=[
            AdminOwnerHotkey(
                miner_hotkey=entry.miner_hotkey,
                miner_coldkeys=list(entry.miner_coldkeys),
                link_hop=entry.link_hop,
                submission_count=entry.submission_count,
                paid_submission_count=entry.paid_submission_count,
                latest_submitted_at=entry.latest_submitted_at,
                agents_truncated=entry.agents_truncated,
                agents=[
                    AdminOwnerAgent(
                        agent_id=agent.agent_id,
                        agent_name=agent.agent_name,
                        agent_version=agent.agent_version,
                        agent_status=agent.agent_status,
                        artifact_sha256=agent.artifact_sha256,
                        submitted_at=agent.submitted_at,
                        miner_coldkey=agent.miner_coldkey,
                    )
                    for agent in entry.agents
                ],
            )
            for entry in footprint.hotkeys
        ],
    )
