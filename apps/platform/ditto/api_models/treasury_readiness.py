"""Bounded operator diagnostics; a shadow observation is never funding readiness."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ditto_screening_protocol.treasury import TreasuryEmissionPolicy, TreasuryLedgerPin

ObserverStatus = Literal[
    "disabled", "not_observed", "observing", "observed", "unavailable"
]
TreasuryBlockReason = Literal[
    "producer_disabled",
    "no_epoch_pin",
    "stored_pin_invalid",
    "proposal_pin_mismatch",
    "shadow_only",
    "offline_policy_unverified",
    "weight_adapter_not_active",
    "fleet_gate_unimplemented",
    "current_epoch_not_checked",
]


class TreasuryLedgerReadiness(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    configured_proposal: TreasuryEmissionPolicy | None
    observer_status: ObserverStatus
    observer_scope: Literal["this_platform_process"] = "this_platform_process"
    latest_stored_epoch_index: int | None
    latest_stored_ledger_digest: str | None
    stored_shadow_pin: TreasuryLedgerPin | None
    blocking_reasons: list[TreasuryBlockReason]
    offline_policy_verified: Literal[False] = False
    weight_effect: Literal["none"] = "none"
    can_enforce_weights: Literal[False] = False
