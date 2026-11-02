"""Explicit read-only projection of revealed and pending chain weight evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.public import PublicChainEpoch, PublicValidatorWeightVector
from ditto.api_models.upload import _SS58_PATTERN

Counter = Annotated[int, Field(ge=0)]
U16 = Annotated[int, Field(ge=0, le=65535)]
Tempo = Annotated[int, Field(ge=1, le=50_400)]


class ValidatorWeightObservation(PublicValidatorWeightVector):
    validator_trust_u16: U16
    validator_trust: Annotated[float, Field(ge=0, le=1)]
    last_update_block: Counter


class PendingWeightObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    commit_epoch: Counter
    commit_block: Counter
    reveal_round: Counter
    commit_block_timestamp: Annotated[
        Counter | None,
        Field(
            description=(
                "Unix seconds of `Timestamp.Now` at the commit block, or null "
                "when that block's state was unreadable."
            )
        ),
    ]
    implied_reveal_block: Annotated[
        Counter | None,
        Field(
            description=(
                "Block the committer's drand round points at, derived as "
                "commit_block + (round time - commit block time) / 12 to about "
                "one block. A stateful drand 2.0 commit targets the boundary "
                "ending its epoch plus 3; a legacy tempo+1 commit made in the "
                "same epoch can target a block inside that epoch. Null when "
                "commit_block_timestamp is null."
            )
        ),
    ]
    implied_reveal_offset_blocks: Annotated[
        int | None,
        Field(
            description=(
                "implied_reveal_block minus the boundary that ends the commit's "
                "epoch: next_epoch_block for a current-epoch commit, "
                "last_epoch_block for a previous-epoch one. About +3 means the "
                "stateful schedule; a large negative value means the legacy "
                "same-epoch reveal lane. Null for older commits or when "
                "implied_reveal_block is null."
            )
        ),
    ]


class WeightConsensusObservation(BaseModel):
    uid: U16
    value: U16


class ValidatorWeightDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    netuid: U16
    block: Counter
    block_hash: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    last_epoch_block: Counter
    pending_epoch_at: Counter
    subnet_epoch_index: Counter
    tempo: Tempo
    blocks_since_last_step: Counter
    next_epoch_block: Annotated[
        Counter,
        Field(
            description=(
                "First block after `block` at which Subtensor steps this "
                "subnet's epoch, simulated from the stateful counters above "
                "exactly as drand 2.0 and the Pylon image do."
            )
        ),
    ]
    epoch: PublicChainEpoch | None
    validators: Annotated[list[ValidatorWeightObservation], Field(max_length=256)]
    consensus: Annotated[list[WeightConsensusObservation], Field(max_length=65536)]
    pending_commits: Annotated[list[PendingWeightObservation], Field(max_length=2560)]
    # vTrust is the last Yuma result, whereas newer reveals can already have
    # replaced the stored row. One current observation cannot prove causality.
    historical_clipping_verified: Literal[False] = False
    weights_submitted: Literal[False] = False
