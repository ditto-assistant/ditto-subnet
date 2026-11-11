"""Operator view of how far Platform's metagraph snapshot trails the chain."""

from datetime import datetime

from pydantic import BaseModel, Field


class ChainRegistrationRead(BaseModel):
    """The newest registration map Platform read straight from chain storage."""

    block: int = Field(ge=0)
    block_timestamp: datetime | None
    provider: str
    observed_at: datetime


class RegistrationChangeView(BaseModel):
    """A uid whose hotkey differs between Pylon's snapshot and what is served."""

    uid: int = Field(ge=0)
    pylon_hotkey: str | None
    chain_hotkey: str | None


class RegistrationFreshnessView(BaseModel):
    """Pylon's recent-neurons snapshot height against a fresh finalized head.

    Pylon ages its cache by when it wrote it, so only ``snapshot_block`` shows
    a refresh that read a lagging node. When ``chain_registrations`` is newer
    than the snapshot, every consumer (upload admission, public activity, the
    emission ledger) is served its uid/hotkey/coldkey and ``changes`` lists
    the uids it corrected.
    """

    netuid: int
    generated_at: datetime
    snapshot_block: int = Field(ge=0)
    snapshot_observed_at: datetime
    finalized_block: int | None
    finalized_block_error: str | None
    snapshot_lag_blocks: int | None
    lag_reconcile_threshold_blocks: int
    lag_check_interval_seconds: float | None
    chain_registrations: ChainRegistrationRead | None
    serving_registrations_block: int
    changes: list[RegistrationChangeView]
