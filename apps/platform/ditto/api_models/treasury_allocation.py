"""Public treasury intent, deliberately distinct from active weight routing."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ditto.api_models.treasury_settings import TreasuryPayeeRule


class PublicServiceBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket_id: str
    purpose: str
    allocation_bps: int
    holding_coldkey: str | None
    publish_payments: bool
    payee_rules: list[TreasuryPayeeRule]


class PublicTreasuryAllocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policy_revision: int
    allocation_version: Literal[1, 2]
    mode: Literal["shadow"] = "shadow"
    routing_status: Literal["not_activated"] = "not_activated"
    denominator: Literal["miner_emission", "released_miner_emission"]
    service_bps: int
    forecast_service_share: float
    forecast_burn_share: float
    forecast_miner_share: float
    effective_service_share: Literal[0] = 0
    burn_revision: int
    burn_of_miner_remainder: float
    collector_hotkey: str | None
    collector_coldkey: str | None
    sweep_interval_hours: int
    sweep_status: Literal["not_activated"] = "not_activated"
    payment_observer_status: Literal["not_activated"] = "not_activated"
    buckets: list[PublicServiceBucket]
