"""Versioned, shadow-only SN118 treasury allocation proposal.

This contract does not change validator weights or authorize a transfer.  It
keeps the two proposed budgets separate while custody and destination identity
are reviewed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TREASURY_BPS = 500


class TreasurySettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: Literal["shadow"] = "shadow"
    maintenance_bps: Annotated[int, Field(ge=0, le=MAX_TREASURY_BPS)] = 0
    gm_bps: Annotated[int, Field(ge=0, le=MAX_TREASURY_BPS)] = 0
    treasury_hotkey: str | None = None
    treasury_coldkey: str | None = None
    gm_account_ref: str | None = None
    max_daily_outflow_rao: Annotated[int, Field(ge=0)] = 0
    max_single_topup_rao: Annotated[int, Field(ge=0)] = 0
    max_slippage_bps: Annotated[int, Field(ge=0, le=500)] = 0

    @model_validator(mode="after")
    def validate_allocation(self) -> TreasurySettings:
        if self.maintenance_bps + self.gm_bps > MAX_TREASURY_BPS:
            raise ValueError("combined treasury allocation exceeds 500 bps")
        if (self.maintenance_bps or self.gm_bps) and (
            not self.treasury_hotkey or not self.treasury_coldkey
        ):
            raise ValueError("nonzero allocation requires both treasury keys")
        if self.gm_bps and not self.gm_account_ref:
            raise ValueError("GM allocation requires an account reference")
        if self.max_single_topup_rao > self.max_daily_outflow_rao:
            raise ValueError("single top-up cannot exceed daily outflow limit")
        return self

    @property
    def miner_bps(self) -> int:
        return 10_000 - self.maintenance_bps - self.gm_bps


class TreasurySettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    settings: TreasurySettings
    checksum: str
    reason: str
    actor: str
    created_at: datetime


class TreasurySettingsControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    effective: TreasurySettings
    revision: int
    miner_bps: int
    history: list[TreasurySettingsRevision]
    weight_effect: Literal["none"] = "none"


class AdminTreasurySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    settings: TreasurySettings
    reason: Annotated[str, Field(min_length=8)]
    confirmation: Literal["RECORD TREASURY SHADOW POLICY"]
