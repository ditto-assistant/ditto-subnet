"""Bounded operator diagnostics; a shadow observation is never funding readiness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ditto_screening_protocol.treasury import (
    Digest,
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)

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
    proposal_approval_status: Literal["not_configured", "verified", "invalid"] = (
        "not_configured"
    )
    proposal_approved_policy_digest: Digest | None = None
    observer_status: ObserverStatus
    observer_scope: Literal["this_platform_process"] = "this_platform_process"
    latest_stored_epoch_index: int | None
    latest_stored_ledger_digest: str | None
    stored_shadow_pin: TreasuryLedgerPin | None
    blocking_reasons: list[TreasuryBlockReason]
    offline_policy_verified: Literal[False] = False
    weight_effect: Literal["none"] = "none"
    can_enforce_weights: Literal[False] = False

    @model_validator(mode="after")
    def proposal_status_binds_digest(self) -> TreasuryLedgerReadiness:
        if (self.proposal_approval_status == "verified") != (
            self.proposal_approved_policy_digest is not None
        ):
            raise ValueError("proposal approval status must bind its exact digest")
        if self.proposal_approval_status == "verified" and (
            self.configured_proposal is None
            or self.configured_proposal.digest != self.proposal_approved_policy_digest
        ):
            raise ValueError("approved digest must bind the configured proposal")
        return self
