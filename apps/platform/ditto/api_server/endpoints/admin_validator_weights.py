"""On-demand vTrust and timelock evidence; no cached success on read failure."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ditto.api_models.public import PublicChainWeight
from ditto.api_models.validator_weight_diagnostics import (
    PendingWeightObservation,
    ValidatorWeightDiagnosticsResponse,
    ValidatorWeightObservation,
    WeightConsensusObservation,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.public import _public_epoch
from ditto.api_server.endpoints.validator import ChainDep
from ditto.chain.errors import ChainError
from ditto.chain.weight_diagnostics import read_weight_diagnostics

router = APIRouter(prefix="/admin", tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/validator-weight-diagnostics", response_model=ValidatorWeightDiagnosticsResponse
)
async def validator_weight_diagnostics(
    request: Request,
    response: Response,
    chain: ChainDep,
    _admin: AdminDep,
    validator_uid: Annotated[int | None, Query(ge=0, le=65535)] = None,
) -> ValidatorWeightDiagnosticsResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        evidence = await read_weight_diagnostics(
            chain, request.app.state.config.chain.netuid
        )
    except ChainError as error:
        raise HTTPException(
            status_code=503, detail="chain weight diagnostics unavailable"
        ) from error
    snapshot = evidence.snapshot
    rows = [
        v
        for v in snapshot.vectors
        if validator_uid is None or v.validator_uid == validator_uid
    ]
    if validator_uid is not None and not rows:
        raise HTTPException(
            status_code=404, detail="validator has no revealed weight row"
        )
    if any(
        v.validator_uid >= len(evidence.validator_trust)
        or v.validator_uid >= len(evidence.last_updates)
        for v in rows
    ):
        raise HTTPException(
            status_code=503, detail="chain weight evidence is incomplete"
        )
    hotkeys = {v.validator_hotkey for v in rows}
    return ValidatorWeightDiagnosticsResponse(
        netuid=snapshot.netuid,
        block=snapshot.block,
        block_hash=snapshot.block_hash,
        last_epoch_block=evidence.last_epoch_block,
        pending_epoch_at=evidence.pending_epoch_at,
        subnet_epoch_index=evidence.subnet_epoch_index,
        epoch=_public_epoch(snapshot),
        validators=[
            ValidatorWeightObservation(
                validator_uid=v.validator_uid,
                validator_hotkey=v.validator_hotkey,
                weights=[
                    PublicChainWeight(uid=w.uid, hotkey=w.hotkey, value=w.value)
                    for w in v.weights
                ],
                validator_trust_u16=evidence.validator_trust[v.validator_uid],
                validator_trust=evidence.validator_trust[v.validator_uid] / 65535,
                last_update_block=evidence.last_updates[v.validator_uid],
            )
            for v in rows
        ],
        consensus=[
            WeightConsensusObservation(uid=uid, value=value)
            for uid, value in enumerate(evidence.consensus)
            if value
        ],
        pending_commits=[
            PendingWeightObservation(
                validator_hotkey=c.hotkey,
                commit_epoch=c.epoch,
                commit_block=c.commit_block,
                reveal_round=c.reveal_round,
            )
            for c in evidence.pending
            if validator_uid is None or c.hotkey in hotkeys
        ],
    )
