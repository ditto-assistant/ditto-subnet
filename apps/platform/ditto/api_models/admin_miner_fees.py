"""Authenticated operator accounting for miner submission fees."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MinerFeeDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    paid_submissions: int = Field(ge=0)
    gross_amount_rao: int = Field(ge=0)
    priced_submissions: int = Field(ge=0)
    gross_value_usd: Decimal = Field(ge=0)


class AdminMinerFeeSummary(BaseModel):
    """Ledger-derived gross revenue; wallet holdings are intentionally separate."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    payment_address: str
    paid_submissions: int = Field(ge=0)
    gross_amount_rao: int = Field(ge=0)
    priced_submissions: int = Field(ge=0)
    unpriced_submissions: int = Field(ge=0)
    gross_value_usd: Decimal = Field(ge=0)
    unique_paying_coldkeys: int = Field(ge=0)
    first_payment_at: datetime | None
    last_payment_at: datetime | None
    recent_days: list[MinerFeeDay]
