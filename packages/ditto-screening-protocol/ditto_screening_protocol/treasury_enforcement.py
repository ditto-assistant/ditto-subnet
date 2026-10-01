"""Future enforcing contract; shape alone is never dispatch authorization.

V1 remains shadow-only. V2 requires an exact offline proposal proof, immutable
epoch identity and a nonempty complete weight-setter capability snapshot. A
consumer must additionally verify crypto and current finalized identity. No
producer or configuration binding is enabled by defining this contract.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .treasury import (
    Address,
    Block,
    Digest,
    Hash,
    TreasuryCollectorIdentity,
    TreasuryEmissionPolicy,
    TreasuryLedgerPin,
)
from .treasury_approval import (
    TreasuryPolicyApproval,
    verify_policy_approval,
    verify_public_signature,
)

TREASURY_WEIGHT_PROTOCOL = 30


class TreasuryWeightCapability(BaseModel):
    """Exact policy and queued guard implemented by the reporting runtime."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    treasury_pin_version: Literal[2]
    treasury_dispatch_version: Literal[2]
    approved_policy_digest: Digest
    collector_policy_digest: Digest

    @field_validator("treasury_pin_version", "treasury_dispatch_version", mode="before")
    @classmethod
    def integer_versions(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("treasury capability versions must be integers")
        return value


class TreasuryFleetMember(TreasuryWeightCapability):
    """Projection of one fresh authenticated weight-setter heartbeat."""

    validator_hotkey: Address
    protocol_version: Annotated[int, Field(ge=TREASURY_WEIGHT_PROTOCOL)]


class EnforcingTreasuryPin(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    version: Literal[2] = 2
    mode: Literal["enforce"] = "enforce"
    epoch_index: Block
    first_block: Block
    pinned_block: Block
    pinned_block_hash: Hash
    policy: TreasuryEmissionPolicy
    policy_digest: Digest
    identity: TreasuryCollectorIdentity
    approval: TreasuryPolicyApproval
    fleet: Annotated[
        tuple[TreasuryFleetMember, ...], Field(min_length=1, max_length=4096)
    ]

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("treasury pin version must be an integer")
        return value

    @field_validator("fleet", mode="before")
    @classmethod
    def immutable_fleet(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def coherent_frozen_evidence(self) -> EnforcingTreasuryPin:
        if (
            self.policy_digest != self.policy.digest
            or self.approval.policy != self.policy
        ):
            raise ValueError("enforcing policy differs from approval or digest")
        expected = (
            self.policy.genesis_hash,
            self.policy.netuid,
            self.policy.collector_hotkey,
            self.policy.collector_coldkey,
        )
        actual = (
            self.identity.genesis_hash,
            self.identity.netuid,
            self.identity.hotkey,
            self.identity.owner_coldkey,
        )
        if expected != actual:
            raise ValueError("enforcing identity differs from approved policy")
        if not self.first_block <= self.identity.finalized_block <= self.pinned_block:
            raise ValueError("enforcing identity is outside the pinned epoch")
        if (
            self.identity.finalized_block == self.pinned_block
            and self.identity.finalized_block_hash != self.pinned_block_hash
        ):
            raise ValueError("enforcing identity hash differs from pin")
        keys = tuple(member.validator_hotkey for member in self.fleet)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("fleet must be nonempty, unique and sorted")
        if any(
            member.approved_policy_digest != self.policy_digest
            or member.collector_policy_digest != self.policy.collector_policy_digest
            for member in self.fleet
        ):
            raise ValueError("fleet approval differs from enforcing policy")
        return self


def require_treasury_weight_authority(
    pin: EnforcingTreasuryPin,
    *,
    expected_policy_digest: str,
    expected_collector_policy_digest: str,
    local_capability: TreasuryFleetMember,
    current_identity: TreasuryCollectorIdentity,
    netuid: int,
    current_epoch_index: int,
    current_first_block: int,
    finalized_block: int,
    finalized_block_hash: str,
) -> EnforcingTreasuryPin:
    """Validate supplied observations, never a caller's verified boolean.

    The production caller must fetch the current identity and epoch itself at
    one finalized hash; parameters alone do not attest their chain origin.
    """
    pin = EnforcingTreasuryPin.model_validate(pin)
    current = TreasuryCollectorIdentity.model_validate(current_identity)
    member = TreasuryFleetMember.model_validate(local_capability)
    verify_policy_approval(
        pin.approval,
        expected_policy_digest=expected_policy_digest,
        expected_collector_policy_digest=expected_collector_policy_digest,
        verify_signature=verify_public_signature,
    )
    if any(
        type(value) is not int or value < 0
        for value in (
            netuid,
            current_epoch_index,
            current_first_block,
            finalized_block,
        )
    ):
        raise ValueError("treasury dispatch heights must be unsigned integers")
    if (netuid, current_epoch_index, current_first_block) != (
        pin.policy.netuid,
        pin.epoch_index,
        pin.first_block,
    ):
        raise ValueError("treasury dispatch is outside the immutable epoch")
    if (
        current.finalized_block != finalized_block
        or current.finalized_block_hash != finalized_block_hash
    ):
        raise ValueError("collector observation is not the current finalized head")
    if finalized_block < pin.pinned_block:
        raise ValueError("collector observation predates the immutable pin")
    if (
        finalized_block == pin.pinned_block
        and finalized_block_hash != pin.pinned_block_hash
    ):
        raise ValueError("collector finalized hash differs from pinned head")
    identity_fields = (
        "genesis_hash",
        "netuid",
        "uid",
        "hotkey",
        "owner_coldkey",
        "uid_hotkey",
        "subnet_owner_coldkey",
    )
    if any(
        getattr(current, name) != getattr(pin.identity, name)
        for name in identity_fields
    ):
        raise ValueError("collector identity drift requires an audited rebind")
    if member not in pin.fleet:
        raise ValueError("this validator is absent or differs from pinned fleet")
    return pin


# Both versions validate the exact integer version before coercion. Pydantic's
# field discriminator disallows those validators; the validated union keeps the
# historical V1 rule intact and each branch still requires its own literal.
TreasuryPin = TreasuryLedgerPin | EnforcingTreasuryPin
