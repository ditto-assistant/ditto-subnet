"""Public Gamma ledger: what the collector earned, holds, locked and moved.

Every amount is alpha in rao (1e-9 alpha) read from finalized public chain
state. An outflow is attributed only by a verified treasury receipt; the
remainder is reported as unattributed until Backroom records the receipt.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TreasuryLedgerHoldingView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket_id: str
    coldkey: str
    alpha_total_rao: int
    tao_free_rao: int


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
    earned_per_epoch_rao: int | None
    total_rao: int
    locked_rao: int
    available_rao: int
    lock_status: Literal["none", "perpetual", "decaying"]
    owner_hotkey: str | None
    owner_conviction_rao: int | None
    owner_holds_most_conviction: bool | None
    holdings: list[TreasuryLedgerHoldingView]


class TreasuryLedgerInterval(BaseModel):
    """The change between two consecutive samples."""

    model_config = ConfigDict(extra="ignore")

    epoch_index: int
    previous_epoch_index: int
    block_number: int
    block_at: datetime
    earned_rao: int | None
    estimated: bool
    """True when samples skipped epochs and earnings were extrapolated."""
    balance_change_rao: int
    outflow_rao: int
    attributed_rao: int
    unattributed_rao: int
    receipt_ids: list[int]


class TreasuryLedgerTotals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    window_epochs: int
    earned_rao: int
    outflow_rao: int
    attributed_rao: int
    unattributed_rao: int


class TreasuryLedgerSamplerStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool
    state: Literal["running", "not_configured", "blocked", "stopped"]
    last_sampled_at: datetime | None = None
    last_error: str | None = None


class PublicTreasuryLedger(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current: TreasuryLedgerCurrent | None
    totals: TreasuryLedgerTotals
    intervals: list[TreasuryLedgerInterval] = Field(default_factory=list)
    sampler: TreasuryLedgerSamplerStatus
