"""Authenticated read of the metagraph snapshot's height against the chain."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ditto.api_models.registration_freshness import (
    ChainRegistrationRead,
    RegistrationChangeView,
    RegistrationFreshnessView,
)
from ditto.api_server.dependencies import get_chain_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.chain import ChainClient, ChainError
from ditto.chain.client import SNAPSHOT_LAG_RECONCILE_BLOCKS

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/registration-freshness", response_model=RegistrationFreshnessView)
async def get_registration_freshness(
    _admin: Annotated[None, Depends(require_admin)],
    request: Request,
    response: Response,
    chain: Annotated[ChainClient, Depends(get_chain_client)],
) -> RegistrationFreshnessView:
    """Read Pylon's snapshot block, a fresh finalized head and any correction.

    One finalized header read; never starts a chain-storage registration read
    and changes nothing.
    """
    response.headers["Cache-Control"] = "no-store"
    config = request.app.state.config.chain
    try:
        freshness = await chain.get_registration_freshness(config.netuid)
    except ChainError as e:
        raise HTTPException(
            status_code=503, detail="metagraph snapshot unavailable; retry shortly"
        ) from e
    registrations = freshness.chain_registrations
    return RegistrationFreshnessView(
        netuid=freshness.netuid,
        generated_at=datetime.now(UTC),
        snapshot_block=freshness.snapshot_block,
        snapshot_observed_at=freshness.snapshot_observed_at,
        finalized_block=freshness.finalized_block,
        finalized_block_error=freshness.finalized_block_error,
        snapshot_lag_blocks=(
            None
            if freshness.finalized_block is None
            else freshness.finalized_block - freshness.snapshot_block
        ),
        lag_reconcile_threshold_blocks=SNAPSHOT_LAG_RECONCILE_BLOCKS,
        lag_check_interval_seconds=config.metagraph_lag_check_seconds,
        chain_registrations=(
            None
            if registrations is None
            else ChainRegistrationRead(
                block=registrations.block,
                block_timestamp=(
                    None
                    if registrations.block_timestamp is None
                    else datetime.fromtimestamp(registrations.block_timestamp, UTC)
                ),
                provider=registrations.provider,
                observed_at=registrations.observed_at,
            )
        ),
        serving_registrations_block=freshness.serving_registrations_block,
        changes=[
            RegistrationChangeView(
                uid=change.uid,
                pylon_hotkey=change.pylon_hotkey,
                chain_hotkey=change.chain_hotkey,
            )
            for change in freshness.changes
        ],
    )
