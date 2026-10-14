"""Public Gamma ledger: what the collector earned, holds, locked and moved.

Every amount is alpha in rao (1e-9 alpha) read from finalized public chain
state. An outflow is attributed only by a verified treasury receipt; the
remainder is reported as unattributed until Backroom records the receipt.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Rao amounts exceed JavaScript's safe integer range at scale; send strings.
Rao = Annotated[int, PlainSerializer(str, return_type=str, when_used="json")]


class TreasuryLedgerHoldingView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket_id: str
    coldkey: str
    alpha_total_rao: Rao
    tao_free_rao: Rao


class TreasuryLedgerCurrent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    epoch_index: int
    block_number: int
    block_hash: str
    block_at: datetime
    collector_hotkey: str
    collector_coldkey: str
    collector_uid: int | None
    collector_incentive: float | None
    earned_per_epoch_rao: Rao | None
    total_rao: Rao
    locked_rao: Rao
    available_rao: Rao
    lock_status: Literal["none", "perpetual", "decaying"]
    owner_hotkey: str | None
    owner_conviction_rao: Rao | None
    owner_holds_most_conviction: bool | None
    holdings: list[TreasuryLedgerHoldingView]


class TreasuryLedgerInterval(BaseModel):
    """The change between two consecutive samples."""

    model_config = ConfigDict(extra="ignore")

    epoch_index: int
    previous_epoch_index: int
    block_number: int
    block_at: datetime
    earned_rao: Rao | None
    estimated: bool
    """True when samples skipped epochs and earnings were extrapolated."""
    balance_change_rao: Rao
    outflow_rao: Rao
    attributed_rao: Rao
    unattributed_rao: Rao
    receipt_count: int
    """Verified receipts in the interval, including unpublished buckets."""
    receipt_ids: list[int]
    """Public treasury event ids of the published receipts."""


class TreasuryLedgerTotals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    window_epochs: int
    earned_rao: Rao
    outflow_rao: Rao
    attributed_rao: Rao
    unattributed_rao: Rao


class TreasuryLedgerSamplerStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool
    state: Literal["running", "not_configured", "blocked", "stopped"]
    last_sampled_at: datetime | None = None
    """When this process last recorded a new epoch sample."""
    last_checked_at: datetime | None = None
    """When this process last read the chain, whether or not the epoch was new."""
    last_error: str | None = None


class PublicTreasuryLedger(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current: TreasuryLedgerCurrent | None
    totals: TreasuryLedgerTotals
    intervals: list[TreasuryLedgerInterval] = Field(default_factory=list)
    sampler: TreasuryLedgerSamplerStatus
