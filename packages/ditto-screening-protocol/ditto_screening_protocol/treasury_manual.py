"""Known-field manual custody requests. Observations are never chain proof."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from .treasury import Address, Digest, Hash
from .treasury_manual_limit import ManualLimitApproval

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


class ManualSourceClaim(Wire):
    source_block: Positive
    amount_rao: Positive


class ManualBatchRequest(ManualRequest):
    sources: Annotated[list[ManualSourceClaim], Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def exact_sources(self):
        blocks = [s.source_block for s in self.sources]
        if (
            blocks != sorted(set(blocks))
            or self.source_block != blocks[0]
            or sum(s.amount_rao for s in self.sources) != self.amount_rao
        ):
            raise ValueError(
                "manual sources must be ordered, unique and conserve amount"
            )
        return self


class ManualBatchEnvelope(ManualEnvelope):
    version: Literal[2] = 2
    request: ManualBatchRequest
    manual_approval: ManualLimitApproval


ManualEnvelopeWire = Annotated[
    ManualEnvelope | ManualBatchEnvelope, Field(discriminator="version")
]


_MANUAL_ENVELOPE_ADAPTER = TypeAdapter(ManualEnvelopeWire)


def parse_manual_envelope(body):
    return _MANUAL_ENVELOPE_ADAPTER.validate_python(body)


class ManualPart(Wire):
    bucket_id: Bucket
    alpha_rao: Positive
    holding_coldkey: Address


class ManualSource(Wire):
    source_block: Positive
    remaining: Annotated[list[ManualPart], Field(max_length=20)]


class ManualUnsignedIntent(Wire):
    request_id: str
    after_operation: Positive
    expires_block: Positive

    _canonical_uuid = field_validator("request_id")(
        ManualRequest.canonical_uuid.__func__
    )


class ManualReadiness(Wire):
    unsigned_intent: ManualUnsignedIntent | None = None
    policy: Digest
    after_operation: Nonnegative
    previous_state: str | None
    bounded_claim_available: bool
    finalized_block: Positive
    available_alpha_rao: Annotated[int, Field(ge=0, le=2**53 - 1, strict=True)]
    max_distribution_rao: Positive
    sources: Annotated[list[ManualSource], Field(max_length=512)]
    manual_approval: ManualLimitApproval | None = None


class ManualSourceEpoch(Wire):
    source_block: Positive
    epoch_index: Nonnegative


class ManualSettlement(Wire):
    epoch_index: Nonnegative
    block: Positive
    block_hash: Hash
    extrinsic_index: Annotated[int, Field(ge=0, strict=True)]
    extrinsic_hash: Hash
    source_epochs: Annotated[list[ManualSourceEpoch], Field(max_length=512)] = []


class ManualBlocker(Wire):
    code: Literal[
        "runtime_changed", "policy_runtime_unreviewed", "observation_unavailable"
    ]
    expected_runtime_hash: Hash | None = None
    observed_runtime_hash: Hash | None = None


class ManualReport(Wire):
    version: Literal[1] = 1
    collector_policy_digest: Digest
    # Generated at the actual observation, not a relay/ack timestamp.
    observed_at: Positive
    readiness: ManualReadiness | None = None
    request_id: str | None = None
    request_digest: Digest | None = None
    status: Literal["readiness", "blocked", "pending", "finalized", "failed", "refused"]
    settlement: ManualSettlement | None = None
    blocker: ManualBlocker | None = None
    failure_code: Literal["signing_window_elapsed", "expired_unsigned"] | None = None

    @model_validator(mode="after")
    def report_shape(self):
        if self.failure_code is not None and self.status != "failed":
            raise ValueError("Only failed requests carry failure diagnostics")
        if self.status == "blocked":
            if self.blocker is None or any(
                x is not None
                for x in (
                    self.readiness,
                    self.request_id,
                    self.request_digest,
                    self.settlement,
                )
            ):
                raise ValueError(
                    "Blocked observation cannot carry readiness or a claim"
                )
            return self
        if self.blocker is not None:
            raise ValueError("Only blocked observations carry a blocker")
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
