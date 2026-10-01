"""Versioned, shadow-only SN118 treasury allocation proposal.

Neither policy version changes validator weights or authorizes a transfer.
Version 1 keeps its original 500 bps ceiling; version 2 describes one
collector and isolated service holding coldkeys under a 1,000 bps
aggregate ceiling.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TREASURY_BPS = 500
MAX_SERVICE_BPS = 1_000
PUBLIC_ADDRESS_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"


def public_wallet_address(value: str | None) -> str | None:
    """Legacy policies allowed free text; never publish it as an address."""
    return value if value and re.fullmatch(PUBLIC_ADDRESS_PATTERN, value) else None


class TreasuryObserverSettings(BaseModel):
    """One exact immutable revision for the receipt observer, not latest policy."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    revision: Annotated[int, Field(gt=0, le=2_147_483_647)]
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    settings: dict[str, Any]


class TreasuryPayeeRule(BaseModel):
    """An exact chain-payment classification, never provider credit proof."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    rule_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
    label: Annotated[str, Field(min_length=3, max_length=80)]
    recipient_coldkey: Annotated[str, Field(pattern=PUBLIC_ADDRESS_PATTERN)]
    asset: Literal["TAO", "SN28_ALPHA", "SN118_ALPHA"] = "TAO"
    recipient_hotkey: Annotated[str, Field(pattern=PUBLIC_ADDRESS_PATTERN)] | None = (
        None
    )
    enabled: bool = True

    @model_validator(mode="after")
    def validate_stake_target(self) -> TreasuryPayeeRule:
        if self.asset != "TAO" and not self.recipient_hotkey:
            raise ValueError("stake-payment rule requires recipient hotkey")
        if self.asset == "TAO" and self.recipient_hotkey:
            raise ValueError("TAO payment rule cannot match a stake hotkey")
        return self


class TreasuryServiceBucket(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    bucket_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
    purpose: Annotated[str, Field(min_length=8, max_length=160)]
    allocation_bps: Annotated[int, Field(ge=0, le=MAX_SERVICE_BPS)] = 0
    holding_coldkey: str | None = None
    service_account_ref: str | None = None
    publish_payments: bool = True
    payee_rules: list[TreasuryPayeeRule] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_recipient(self) -> TreasuryServiceBucket:
        ids = [rule.rule_id for rule in self.payee_rules]
        matches = [
            (rule.recipient_coldkey, rule.asset, rule.recipient_hotkey)
            for rule in self.payee_rules
            if rule.enabled
        ]
        if len(ids) != len(set(ids)) or len(matches) != len(set(matches)):
            raise ValueError("duplicate or ambiguous payee rule")
        if self.allocation_bps and not self.holding_coldkey:
            raise ValueError("nonzero service allocation requires holding coldkey")
        return self


class TreasurySettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: Literal["shadow"] = "shadow"
    allocation_version: Literal[1, 2] = 1
    sweep_interval_hours: Annotated[int, Field(ge=1, le=168)] = 24
    maintenance_bps: Annotated[int, Field(ge=0, le=MAX_TREASURY_BPS)] = 0
    gm_bps: Annotated[int, Field(ge=0, le=MAX_TREASURY_BPS)] = 0
    service_buckets: list[TreasuryServiceBucket] = Field(
        default_factory=list, max_length=20
    )
    treasury_hotkey: str | None = None
    treasury_coldkey: str | None = None
    gm_account_ref: str | None = None
    max_daily_outflow_rao: Annotated[int, Field(ge=0)] = 0
    max_single_topup_rao: Annotated[int, Field(ge=0)] = 0
    max_slippage_bps: Annotated[int, Field(ge=0, le=500)] = 0

    @model_validator(mode="after")
    def validate_allocation(self) -> TreasurySettings:
        if self.allocation_version == 2:
            # Address fields are public: reject phrases/credentials even in
            # shadow mode. Syntax alone is not custody or checksum proof.
            addresses = [
                self.treasury_hotkey,
                self.treasury_coldkey,
                *(bucket.holding_coldkey for bucket in self.service_buckets),
            ]
            if any(
                address is not None
                and not re.fullmatch(PUBLIC_ADDRESS_PATTERN, address)
                for address in addresses
            ):
                raise ValueError("v2 wallets must be public SS58 address strings")
            if self.maintenance_bps or self.gm_bps:
                raise ValueError("v2 service buckets cannot mix with v1 allocations")
            if self.gm_account_ref:
                raise ValueError("v2 GM account belongs in its service bucket")
            if bool(self.treasury_hotkey) != bool(self.treasury_coldkey):
                raise ValueError("v2 collector requires both hotkey and coldkey")
            if not self.service_buckets:
                raise ValueError("v2 requires at least one service bucket")
            ids = [bucket.bucket_id for bucket in self.service_buckets]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate service bucket ID")
            wallets = [
                bucket.holding_coldkey
                for bucket in self.service_buckets
                if bucket.holding_coldkey
            ]
            if len(wallets) != len(set(wallets)):
                raise ValueError("service buckets must have distinct holding coldkeys")
            if self.treasury_coldkey in wallets:
                raise ValueError("collector coldkey cannot hold a service bucket")
            if any(bucket.allocation_bps for bucket in self.service_buckets) and (
                not self.treasury_hotkey or not self.treasury_coldkey
            ):
                raise ValueError("nonzero service allocation requires collector keys")
            if (
                sum(bucket.allocation_bps for bucket in self.service_buckets)
                > MAX_SERVICE_BPS
            ):
                raise ValueError("combined service allocation exceeds 1000 bps")
            if (
                self.max_daily_outflow_rao
                or self.max_single_topup_rao
                or self.max_slippage_bps
            ):
                raise ValueError(
                    "v1 GM payment bounds cannot authorize v2 service spending"
                )
            return self
        if self.service_buckets:
            raise ValueError("v1 allocation cannot contain service buckets")
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
        if self.allocation_version == 2:
            return 10_000 - sum(
                bucket.allocation_bps for bucket in self.service_buckets
            )
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
