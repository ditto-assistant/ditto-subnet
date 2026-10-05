"""Read-only proposed-policy diagnostics, never activation authority."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ditto_screening_protocol.treasury import Address, Digest
from ditto_screening_protocol.treasury_approval import TreasuryPolicyApproval
from ditto_screening_protocol.treasury_enforcement import TreasuryWeightCapability
from ditto_screening_protocol.treasury_identity import TreasuryDispatchObservation

PREFLIGHT_ROW_LIMIT = 512
TreasurySetterStatus = Literal[
    "ready",
    "missing_heartbeat",
    "inventory_not_checked",
    "heartbeat_outside_window",
    "invalid_heartbeat",
    "missing_guard",
    "unsupported_protocol",
    "policy_mismatch",
]
TreasuryPreflightBlockReason = Literal[
    "chain_unavailable",
    "inventory_truncated",
    "setter_proof_missing",
    "managed_roster_missing",
    "managed_setter_not_permitted",
]


class TreasuryActivationPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    approval: TreasuryPolicyApproval
    expected_policy_digest: Digest
    expected_collector_policy_digest: Digest
    managed_validator_hotkeys: Annotated[
        tuple[Address, ...], Field(max_length=128)
    ] = ()

    @field_validator("managed_validator_hotkeys")
    @classmethod
    def distinct_managed_keys(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("managed validator roster must be distinct")
        return value


class TreasurySetterPreflight(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    validator_hotkey: Address
    required_by_chain: bool
    seen_at: datetime | None
    protocol_version: int | None
    capability: TreasuryWeightCapability | None
    status: TreasurySetterStatus


class TreasuryActivationPreflight(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    checked_at: datetime
    proposed_policy_digest: Digest
    proposed_collector_policy_digest: Digest
    proposal_signature_verified: Literal[True] = True
    configured_policy_matches: bool
    configured_collector_matches: bool
    chain_status: Literal["verified", "unavailable"]
    chain_failure_stage: Literal["identity", "setter_roster"] | None = None
    chain_failure_kind: (
        Literal[
            "timeout",
            "connection",
            "invalid_evidence",
            "reader_unavailable",
            "unavailable",
        ]
        | None
    ) = None
    observation: TreasuryDispatchObservation | None
    required_setter_count: Annotated[int, Field(ge=1, le=4096)] | None
    gate_scope: Literal["managed_validators"] = "managed_validators"
    managed_validator_hotkeys: tuple[Address, ...] = ()
    chain_permitted_setter_count: Annotated[int, Field(ge=1, le=4096)] | None = None
    setters: Annotated[
        list[TreasurySetterPreflight], Field(max_length=PREFLIGHT_ROW_LIMIT)
    ]
    truncated: bool
    fleet_ready_for_proposed_policy: bool
    blocking_reasons: list[TreasuryPreflightBlockReason]
    # This only compares public read evidence. Existing immutable epoch,
    # current identity and producer configuration gates still apply at dispatch.
    weight_effect: Literal["none"] = "none"
    can_enforce_weights: Literal[False] = False
    copy_behavior_verified: Literal[False] = False
