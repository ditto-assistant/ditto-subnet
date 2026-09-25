"""Explicit allowlist for the public treasury receipt feed."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PublicTreasuryEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    payment_id: str
    event_kind: Literal["gm_credit_purchase", "maintenance_bounty"]
    state: Literal["chain_finalized", "reconciled"]
    event_at: datetime
    recorded_at: datetime
    policy_revision: int
    burn_revision: str
    denominator: Literal["miner_emission", "released_miner_emission"]
    allocation_bps: int
    allocated_alpha_rao: int
    route: str
    asset: str
    gross_amount_atomic: int
    realized_amount_atomic: int | None
    public_sender: str
    public_recipient: str
    block_hash: str
    extrinsic_index: int
    event_index: int
    actor_provenance: str
    verification_source: str


class PublicTreasuryEventPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[PublicTreasuryEvent]
    next_before: int | None
