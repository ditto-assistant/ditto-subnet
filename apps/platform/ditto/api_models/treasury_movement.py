"""A wallet movement proves delivery, not historical allocation or GM credit."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol.treasury import Hash


class TreasuryMovementSelector(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    block: int = Field(gt=0)
    block_hash: Hash
    extrinsic_index: int = Field(ge=0)
    extrinsic_hash: Hash
    amount_atomic: int = Field(gt=0, le=2**63 - 1)
    source_block: Literal[None] = None


class PublicTreasuryMovement(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    payment_id: str
    bucket_id: str
    event_kind: Literal["wallet_transfer"] = "wallet_transfer"
    event_at: datetime
    state: Literal["chain_finalized"] = "chain_finalized"
    public_sender: str
    public_recipient: str
    deposit_asset: Literal["SN118_ALPHA"] = "SN118_ALPHA"
    deposit_amount_atomic: str
    credited_usd_nano: Literal[None] = None
    source_attribution: Literal["incomplete"] = "incomplete"
    block: int
    block_hash: Hash
    extrinsic_index: int
    event_index: int
    extrinsic_hash: Hash


class PublicTreasuryMovementPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[PublicTreasuryMovement]
