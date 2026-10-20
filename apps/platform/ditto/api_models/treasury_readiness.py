"""Bounded operator diagnostics; a shadow observation is never funding readiness."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.chain.errors import TreasuryReadStep
from ditto_screening_protocol.treasury import (
    Digest,
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)
from ditto_screening_protocol.treasury_enforcement import EnforcingTreasuryPin

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
    "fleet_not_ready",
    "enforcing_pin_unverified",
]


LedgerPinFailureKind = Literal[
    "timeout",
    "connection",
    "database",
    "validation",
    "value",
    "type",
    "key",
    "runtime",
    "chain",
    "other",
]
LedgerPinStage = Literal[
    "load_or_build",
    "runtime",
    "ledger_context",
    "ledger_snapshot",
    "treasury_observation",
    "treasury_authorization",
    "draft",
    "insert",
]


class LedgerPinFailure(BaseModel):
    """Fixed error kinds and repository source locations, never exception text."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    kind: LedgerPinFailureKind
    location: str | None = Field(default=None, max_length=256)
    read_step: TreasuryReadStep | None = None


class LedgerPinProducerDiagnostic(BaseModel):
    """Process-local producer evidence; reading it never attempts a build."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    scope: Literal["this_platform_process"] = "this_platform_process"
    in_progress: bool = False
    epoch_index: int | None = None
    stage: LedgerPinStage | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    lock_wait_seconds: float | None = Field(default=None, ge=0)
    outcome: Literal["pinned", "unavailable", "error", "cancelled"] | None = None
    failure: LedgerPinFailure | None = None
    last_success_epoch: int | None = None
    last_success_at: datetime | None = None


class LedgerPinLoopDiagnostic(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    task_state: Literal["not_started", "running", "done", "cancelled"]
    in_progress: bool = False
    stage: Literal["settings", "materializer"] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure: LedgerPinFailure | None = None


class TreasuryChainReadDiagnostic(BaseModel):
    """Completed read counts and safe last-failure evidence, never authority."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    scope: Literal["this_platform_process"] = "this_platform_process"
    operation: Literal["epoch_schedule", "requester_activation"]
    attempts: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    last_finished_at: datetime
    last_elapsed_seconds: float = Field(ge=0)
    last_failure_at: datetime | None = None
    last_failure_elapsed_seconds: float | None = Field(default=None, ge=0)
    last_failure_stage: (
        Literal["epoch_schedule", "identity", "setter_roster"] | None
    ) = None
    last_failure_step: TreasuryReadStep | None = None
    last_failure_kind: (
        Literal["timeout", "connection", "invalid_evidence", "unavailable"] | None
    ) = None
    last_failure_request_id: str | None = Field(default=None, max_length=128)


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
    stored_enforcing_pin: EnforcingTreasuryPin | None = None
    enforcement_configured: bool = False
    fleet_gate: Literal["not_checked", "ready", "not_ready"] = "not_checked"
    blocking_reasons: list[TreasuryBlockReason]
    offline_policy_verified: Literal[False] = False
    offline_epoch_verified: bool = False
    weight_effect: Literal["none"] = "none"
    can_enforce_weights: bool = False
    producer: LedgerPinProducerDiagnostic | None = None
    producer_loop: LedgerPinLoopDiagnostic | None = None
    chain_reads: list[TreasuryChainReadDiagnostic] = Field(
        default_factory=list, max_length=2
    )
    # Independent delivery diagnostic: signed authority is not a served ledger.
    ledger_schedule_probe_status: Literal["not_checked", "available", "unavailable"] = (
        "not_checked"
    )
    ledger_schedule_probe_epoch: int | None = None
    ledger_schedule_probe_block: int | None = None
    ledger_schedule_matches_stored_pin: bool | None = None
    ledger_schedule_failure_kind: (
        Literal["timeout", "connection", "reader_unavailable", "unavailable"] | None
    ) = None
    validation_failure_stage: (
        Literal["fleet_binding", "identity", "setter_roster", "authority"] | None
    ) = None
    validation_failure_step: TreasuryReadStep | None = None
    validation_failure_kind: (
        Literal[
            "timeout",
            "connection",
            "invalid_evidence",
            "reader_unavailable",
            "unavailable",
        ]
        | None
    ) = None

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
