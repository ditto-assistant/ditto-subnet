"""Explicit allowlist for the public treasury receipt feed."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PublicTreasuryEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    payment_id: str
    event_kind: Literal["gm_token_deposit", "gm_credit_purchase", "maintenance_bounty"]
    state: Literal["chain_finalized", "reconciled"]
    finalized_event_id: int | None
    event_at: datetime
    recorded_at: datetime
    policy_revision: int
    burn_revision: int
    burn_share_micros: int
    denominator: Literal["miner_emission", "released_miner_emission"]
    maintenance_bps: int
    gm_bps: int
    allocation_bps: int
    allocated_alpha_rao: str
    source_alpha_rao: str
    route: str
    deposit_asset: Literal["TAO", "SN28_ALPHA", "SN118_ALPHA"]
    deposit_amount_atomic: str
    credited_usd_nano: str | None
    bounty_award_id: str | None
    accepted_work_ref: str | None
    public_sender: str
    public_recipient: str
    block_hash: str
    extrinsic_index: int
    event_index: int
    actor_provenance: str
    actor_public_id: str
    verification_source: str


class PublicTreasuryEventPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[PublicTreasuryEvent]
    next_before: int | None
