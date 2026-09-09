"""Explicit read-only projection of revealed and pending chain weight evidence."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.public import PublicChainEpoch, PublicValidatorWeightVector
from ditto.api_models.upload import _SS58_PATTERN

Counter = Annotated[int, Field(ge=0)]
U16 = Annotated[int, Field(ge=0, le=65535)]


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
    epoch: PublicChainEpoch | None
    validators: Annotated[list[ValidatorWeightObservation], Field(max_length=256)]
    consensus: Annotated[list[WeightConsensusObservation], Field(max_length=65536)]
    pending_commits: Annotated[list[PendingWeightObservation], Field(max_length=2560)]
    # vTrust is the last Yuma result, whereas newer reveals can already have
    # replaced the stored row. One current observation cannot prove causality.
    historical_clipping_verified: Literal[False] = False
    weights_submitted: Literal[False] = False
