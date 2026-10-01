"""Known-field treasury epoch evidence. V1 is shadow-only, never weight authority.

Shape validation binds the recorded observations to the proposed recipient; it
does not fetch or attest chain state. A future producer must independently read
finalized state and gate the whole fleet before an enforcing contract exists.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Hash = Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Address = Annotated[str, Field(pattern=r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")]
Block = Annotated[int, Field(ge=0, strict=True)]


class TreasuryEmissionBucket(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    bucket_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]
    allocation_bps: Annotated[int, Field(ge=0, le=1000)]
    holding_coldkey: Address


class TreasuryEmissionPolicy(BaseModel):
    """Public fold inputs only; no billing references, credentials or seed data."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    version: Literal[1] = 1
    revision: Annotated[int, Field(ge=1)]
    genesis_hash: Hash
    netuid: Literal[118] = 118
    collector_hotkey: Address
    collector_coldkey: Address
    collector_policy_digest: Digest
    buckets: Annotated[
        tuple[TreasuryEmissionBucket, ...], Field(min_length=1, max_length=20)
    ]

    @field_validator("version", "netuid", mode="before")
    @classmethod
    def integer_markers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("treasury version and netuid must be integers")
        return value

    @field_validator("buckets", mode="before")
    @classmethod
    def immutable_buckets(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def bounded_unique_destinations(self) -> TreasuryEmissionPolicy:
        if self.collector_hotkey == self.collector_coldkey:
            raise ValueError("collector hotkey and coldkey must be distinct")
        if len({b.bucket_id for b in self.buckets}) != len(self.buckets):
            raise ValueError("duplicate treasury bucket")
        wallets = [b.holding_coldkey for b in self.buckets]
        if len(set(wallets)) != len(wallets) or {
            self.collector_hotkey,
            self.collector_coldkey,
        }.intersection(wallets):
            raise ValueError("holding coldkeys must be distinct from collector")
        if self.service_bps > 1000:
            raise ValueError("combined service allocation exceeds 1000 bps")
        return self

    @property
    def service_bps(self) -> int:
        return sum(bucket.allocation_bps for bucket in self.buckets)

    @property
    def digest(self) -> str:
        body = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(
            ("ditto-treasury-emission-policy-v1:" + body).encode()
        ).hexdigest()


class TreasuryCollectorIdentity(BaseModel):
    """Finalized read values, not a caller's unbound verified=True assertion."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    genesis_hash: Hash
    netuid: Literal[118] = 118
    finalized_block: Block
    finalized_block_hash: Hash
    uid: Annotated[int, Field(ge=0, le=65535)]
    hotkey: Address
    owner_coldkey: Address
    uid_hotkey: Address
    subnet_owner_coldkey: Address

    @field_validator("netuid", mode="before")
    @classmethod
    def integer_netuid(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("treasury netuid must be an integer")
        return value

    @model_validator(mode="after")
    def reciprocal_non_owner_binding(self) -> TreasuryCollectorIdentity:
        if self.hotkey != self.uid_hotkey:
            raise ValueError("collector UID was reused or does not bind hotkey")
        if self.owner_coldkey == self.subnet_owner_coldkey:
            raise ValueError("collector cannot be associated with subnet owner")
        return self


class TreasuryLedgerPin(BaseModel):
    """V1 can record shadow evidence only. No weight or spending activation."""

    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, revalidate_instances="always"
    )

    version: Literal[1] = 1
    mode: Literal["shadow"] = "shadow"
    policy: TreasuryEmissionPolicy
    policy_digest: Digest
    identity: TreasuryCollectorIdentity

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("treasury version must be an integer")
        return value

    @model_validator(mode="after")
    def bind_policy_identity(self) -> TreasuryLedgerPin:
        if self.policy_digest != self.policy.digest:
            raise ValueError("treasury policy digest mismatch")
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
            raise ValueError("finalized collector identity differs from policy")
        return self

    def require_epoch(
        self, *, netuid: int, first_block: int, pinned_block: int
    ) -> None:
        if self.policy.netuid != netuid:
            raise ValueError("treasury pin belongs to another subnet")
        if not first_block <= self.identity.finalized_block <= pinned_block:
            raise ValueError("treasury identity is stale or from a future block")
