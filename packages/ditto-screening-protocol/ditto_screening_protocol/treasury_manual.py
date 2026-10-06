"""Known-field manual custody requests. Observations are never chain proof."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .treasury import Address, Digest, Hash

Positive = Annotated[int, Field(gt=0, le=2**53 - 1, strict=True)]
Nonnegative = Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
Bucket = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")]


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


class ManualRequest(Wire):
    request_id: str
    after_operation: Positive
    source_block: Positive
    bucket_id: Bucket
    amount_rao: Positive
    retained_alpha_rao: Positive
    expires_block: Positive
    reason: Annotated[str, Field(min_length=8, max_length=240)]

    @field_validator("request_id")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("canonical request UUID required")
        return value

    @field_validator("reason")
    @classmethod
    def audit_reason(cls, value):
        if value != value.strip() or len(value) < 8:
            raise ValueError("trimmed audit reason required")
        return value


class ManualEnvelope(Wire):
    version: Literal[1] = 1
    collector_policy_digest: Digest
    destination: Address
    request: ManualRequest

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


class ManualPart(Wire):
    bucket_id: Bucket
    alpha_rao: Positive
    holding_coldkey: Address


class ManualSource(Wire):
    source_block: Positive
    remaining: Annotated[list[ManualPart], Field(max_length=20)]


class ManualReadiness(Wire):
    policy: Digest
    after_operation: Nonnegative
    previous_state: str | None
    bounded_claim_available: bool
    finalized_block: Positive
    available_alpha_rao: Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
    max_distribution_rao: Positive
    sources: Annotated[list[ManualSource], Field(max_length=100)]


class ManualSettlement(Wire):
    epoch_index: Nonnegative
    block: Positive
    block_hash: Hash
    extrinsic_index: Annotated[int, Field(ge=0, strict=True)]
    extrinsic_hash: Hash


class ManualReport(Wire):
    version: Literal[1] = 1
    collector_policy_digest: Digest
    # Generated at the actual observation, not a relay/ack timestamp.
    observed_at: Positive
    readiness: ManualReadiness | None = None
    request_id: str | None = None
    request_digest: Digest | None = None
    status: Literal["readiness", "pending", "finalized", "failed", "refused"]
    settlement: ManualSettlement | None = None

    @model_validator(mode="after")
    def report_shape(self):
        if self.status == "readiness":
            if (
                self.readiness is None
                or self.request_id is not None
                or self.request_digest is not None
                or self.settlement is not None
            ):
                raise ValueError("Readiness must contain only the observation")
        else:
            if (
                self.request_id is None
                or self.request_digest is None
                or self.readiness is not None
            ):
                raise ValueError("Request result must identify its exact request")
            if str(UUID(self.request_id)) != self.request_id:
                raise ValueError("Canonical result UUID required")
            if (self.status == "finalized") != (self.settlement is not None):
                raise ValueError("Only finalized results carry settlement coordinates")
        return self
