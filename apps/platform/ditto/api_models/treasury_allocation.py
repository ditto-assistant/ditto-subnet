"""Public treasury policy plus the routing the newest served ledger pin enforces."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.treasury_settings import TreasuryPayeeRule


class PublicServiceBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket_id: str
    purpose: str
    allocation_bps: int
    holding_coldkey: str | None
    publish_payments: bool
    payee_rules: list[TreasuryPayeeRule]


class PublicEmissionShare(BaseModel):
    """One recipient's share of total miner emission."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["burn", "track", "gamma"]
    id: str | None
    """Track id (memory, generator) or Gamma bucket id; ``None`` for burn."""
    share: float
    """Fraction of total miner emission, 0..1."""
    bps: float
    """The same share in basis points (float: validators fold in float)."""


class PublicTreasuryRuntime(BaseModel):
    """Recorded control only; no chain dispatch, payment or signer authority."""

    model_config = ConfigDict(extra="ignore")

    revision: int = Field(default=0, ge=0)
    mode: Literal["not_recorded", "observe", "enforce", "pause", "unavailable"] = (
        "not_recorded"
    )
    activation_epoch: int | None = Field(default=None, ge=0)
    allocation_matches: bool | None = None


class PublicTreasuryAllocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policy_revision: int
    allocation_version: Literal[1, 2]
    # Derived from the recorded runtime: "shadow" when none is recorded or it
    # cannot be verified.
    mode: Literal["shadow", "observe", "enforce", "pause"] = "shadow"
    # "active" only when the newest served ledger pin is fresh and carries an
    # enforcing treasury pin, i.e. validators were handed the service split to
    # fold. "stale" means that pin is from an earlier epoch, which validators
    # refuse to fold.
    routing_status: Literal[
        "not_activated", "scheduled", "active", "stale", "paused"
    ] = "not_activated"
    routing_epoch: int | None = Field(default=None, ge=0)
    denominator: Literal["miner_emission", "released_miner_emission"]
    service_bps: int
    forecast_service_share: float
    forecast_burn_share: float
    forecast_miner_share: float
    # The service share of miner emission the active pin folds; 0 otherwise.
    effective_service_share: float = Field(default=0.0, ge=0.0, le=1.0)
    burn_revision: int
    burn_of_miner_remainder: float
    collector_hotkey: str | None
    collector_coldkey: str | None
    sweep_interval_hours: int
    sweep_status: Literal["not_activated"] = "not_activated"
    payment_observer_status: Literal["not_activated"] = "not_activated"
    buckets: list[PublicServiceBucket]
    # Runtime control is not finalized funding proof; receipts are.
    runtime: PublicTreasuryRuntime = Field(default_factory=PublicTreasuryRuntime)
    # SN-350: the whole split of total miner emission per recipient (burn,
    # each paid competition track, each Gamma service bucket), derived from
    # the same fields validators are served. See docs/public-telemetry.md.
    emission_allocation_revision: int = Field(default=0, ge=0)
    emission_split: list[PublicEmissionShare] = Field(default_factory=list)
    emission_split_epoch: int | None = Field(default=None, ge=0)
    emission_split_source: Literal["epoch_pin", "settings", "unavailable"] = (
        "unavailable"
    )
    forecast_split: list[PublicEmissionShare] = Field(default_factory=list)
