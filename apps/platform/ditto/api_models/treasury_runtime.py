"""Durable public-proof control; funds and signer timers remain separate."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto_screening_protocol.treasury import Address, Digest
from ditto_screening_protocol.treasury_approval import TreasuryPolicyApproval


class TreasuryRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    version: Literal[1] = 1
    mode: Literal["observe", "enforce", "pause"]
    approval: TreasuryPolicyApproval
    approved_policy_digest: Digest
    collector_policy_digest: Digest
    managed_validator_hotkeys: Annotated[
        tuple[Address, ...], Field(min_length=1, max_length=128)
    ]
    activation_epoch: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def binds_policy(self):
        if len(set(self.managed_validator_hotkeys)) != len(
            self.managed_validator_hotkeys
        ):
            raise ValueError("managed validator roster must be distinct")
        if self.approval.policy.digest != self.approved_policy_digest or (
            self.approval.policy.collector_policy_digest != self.collector_policy_digest
        ):
            raise ValueError("runtime digests differ from approved policy")
        if (self.mode == "enforce") != (self.activation_epoch is not None):
            raise ValueError("only enforce mode binds an activation epoch")
        return self


class TreasuryRuntimeRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    settings: TreasuryRuntimeSettings
    checksum: Digest
    actor: str
    reason: str
    created_at: datetime


class TreasuryRuntimeControl(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    latest: TreasuryRuntimeRevision | None
    # Configuration is not proof that an epoch was produced or dispatched.
    can_enforce_weights: Literal[False] = False
    transfers_enabled: Literal[False] = False


class AdminTreasuryRuntimeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    settings: TreasuryRuntimeSettings
    reason: Annotated[str, Field(min_length=8)]
    confirmation: Annotated[str, Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def exact_confirmation(self):
        expected = (
            f"GAMMA {self.settings.mode.upper()} {self.settings.approved_policy_digest}"
        )
        if self.confirmation != expected or len(self.reason.strip()) < 8:
            raise ValueError("exact mode and policy confirmation and reason required")
        return self
