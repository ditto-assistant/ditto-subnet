"""Canonical private Bench v9 confirmation transport models.

Platform serves these models and the validator consumes them.  Keeping their
profiles, field order, and semantic validators in this dependency-light module
means both processes execute the same Python classes instead of merely exposing
similar JSON Schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ditto_screening_protocol.confirmation import (
    AblationBudget,
    AblationDimensionEnvelope,
    ConfirmationCompletionReport,
    LongMemDimensionEnvelope,
    Sha256,
    UsageCount,
)

SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
MAX_BUNDLE_REQUEST_CAP = 100_000
MAX_BUNDLE_TOKEN_CAP = 100_000_000

SignatureHex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]+$", min_length=2, max_length=512),
]


class ConfirmationBundleMode(StrEnum):
    """Whether bundle issuance is disabled, measured, or ranking-authoritative."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class ConfirmationProviderLaneProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    lane: Annotated[str, Field(min_length=1, max_length=128)]
    provider: Annotated[str, Field(min_length=1, max_length=128)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    max_requests: Annotated[int, Field(ge=1)]
    max_prompt_tokens: Annotated[int, Field(ge=0)]
    max_completion_tokens: Annotated[int, Field(ge=0)]
    max_total_tokens: Annotated[int, Field(ge=0)]
    max_cost_usd_micros: Annotated[int, Field(ge=0)]


class ConfirmationAblationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    intervention: Literal["inference", "embedding"]
    contract_version: Annotated[str, Field(min_length=1, max_length=128)]
    threshold_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    budget: AblationBudget


class ConfirmationAblationCoordinatorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sample_size: Annotated[int, Field(gt=0)]
    max_attempts: Annotated[int, Field(gt=0)]
    max_requests: Annotated[int, Field(gt=0)]
    request_timeout_milliseconds: Annotated[int, Field(gt=0)]
    total_timeout_milliseconds: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def request_and_time_caps_are_consistent(
        self,
    ) -> ConfirmationAblationCoordinatorProfile:
        minimum_requests = self.sample_size * 3
        if (
            not minimum_requests
            <= self.max_requests
            <= (minimum_requests * self.max_attempts)
        ):
            raise ValueError("ablation coordinator request cap is inconsistent")
        if self.total_timeout_milliseconds < self.request_timeout_milliseconds:
            raise ValueError(
                "ablation coordinator total timeout is shorter than one request"
            )
        return self


class ConfirmationCompositeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    formula_revision: Annotated[str, Field(min_length=1, max_length=128)]
    base_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    longmem_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    checksum: Sha256


class ConfirmationExecutionProfile(BaseModel):
    """Complete non-secret profile needed by the trusted local executor."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    checksum: Sha256
    longmem_profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    longmem_profile_checksum: Sha256
    longmem_dataset_revision: Annotated[str, Field(min_length=1, max_length=128)]
    longmem_dataset_sha256: Sha256
    longmem_selector_revision: Literal["longmemeval-s-stratified-sha256-v1"]
    longmem_selection_seed: Annotated[int, Field(ge=0, le=(1 << 64) - 1)]
    longmem_cases_per_capability: Annotated[int, Field(ge=2)]
    longmem_seed_batch_pairs: Annotated[int, Field(gt=0)]
    longmem_projection_key_sha256: Sha256
    provider_lanes: list[ConfirmationProviderLaneProfile]
    ablation_profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    ablation_profile_checksum: Sha256
    ablation_dataset_sha256: Sha256
    ablation_threshold_manifest_sha256: Sha256
    ablation_selection_key_sha256: Sha256
    ablation_projection_key_sha256: Sha256
    ablation_coordinator_policy: ConfirmationAblationCoordinatorProfile
    inference_ablation: ConfirmationAblationProfile
    embedding_ablation: ConfirmationAblationProfile
    composite: ConfirmationCompositeProfile

    @model_validator(mode="after")
    def ablation_roles_are_not_swappable(self) -> ConfirmationExecutionProfile:
        if self.inference_ablation.intervention != "inference":
            raise ValueError("inference_ablation must use the inference intervention")
        if self.embedding_ablation.intervention != "embedding":
            raise ValueError("embedding_ablation must use the embedding intervention")
        return self


class V9ConfirmationClaimRequest(BaseModel):
    # FastAPI receives UUIDs and datetimes from JSON as strings.  Field-level
    # constraints still reject malformed values; model-wide strict mode would
    # make this otherwise valid JSON transport impossible to call.
    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    slot_id: Annotated[str, Field(pattern=r"^slot-[0-7]$")]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    profile_checksum: Sha256
    nonce: UUID
    requested_at: datetime
    signature: SignatureHex

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class V9ConfirmationJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: Literal["v9_confirmation_bundle"]
    bundle_id: UUID
    ticket_id: UUID
    reservation_id: UUID
    agent_id: UUID
    slot_id: Annotated[str, Field(pattern=r"^slot-[0-7]$")]
    deadline: datetime
    artifact_sha256: Sha256
    bench_version: Literal[9]
    settings_revision: Annotated[int, Field(ge=1)]
    settings_checksum: Sha256
    retest_generation: Annotated[int, Field(ge=0)]
    mode: ConfirmationBundleMode
    per_bundle_request_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_REQUEST_CAP)]
    per_bundle_token_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_TOKEN_CAP)]
    execution_profile: ConfirmationExecutionProfile

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        return value


V9ConfirmationCompletionReport = ConfirmationCompletionReport


class V9ConfirmationSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    ticket_id: UUID
    report: ConfirmationCompletionReport


class V9ConfirmationRawDimension(BaseModel):
    """One exact native Go evidence wrapper returned by the local scorer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    go_evidence_sha256: Sha256
    latency_ms: UsageCount
    evidence: dict[str, object]


class V9ConfirmationPrepareRequest(BaseModel):
    """Authenticated native Go evidence awaiting Platform normalization."""

    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    ticket_id: UUID
    nonce: UUID
    requested_at: datetime
    wire_sha256: Sha256
    ablation_coordinator_latency_ms: UsageCount
    longmemeval: V9ConfirmationRawDimension
    inference_ablation: V9ConfirmationRawDimension
    embedding_ablation: V9ConfirmationRawDimension
    signature: SignatureHex

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class V9ConfirmationPreparedReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    ablation_coordinator_latency_ms: UsageCount
    longmemeval: LongMemDimensionEnvelope
    inference_ablation: AblationDimensionEnvelope
    embedding_ablation: AblationDimensionEnvelope
    evidence_sha256: Sha256


class V9ConfirmationFailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    ticket_id: UUID
    reason: Literal["execution_failed", "deadline", "cancelled", "infrastructure"]
    nonce: UUID
    requested_at: datetime
    signature: SignatureHex

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class V9ConfirmationFailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    accepted: Literal[True]
    state: Literal["failed"]
    settled_microusd: Annotated[int, Field(ge=0)]
    replayed: bool


class V9ConfirmationSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    accepted: Literal[True]
    state: Literal["completed"]
    qualification_status: Literal["qualified", "unqualified"]
    evidence_sha256: Sha256
    replayed: bool


__all__ = [
    "ConfirmationAblationCoordinatorProfile",
    "ConfirmationAblationProfile",
    "ConfirmationBundleMode",
    "ConfirmationCompositeProfile",
    "ConfirmationExecutionProfile",
    "ConfirmationProviderLaneProfile",
    "MAX_BUNDLE_REQUEST_CAP",
    "MAX_BUNDLE_TOKEN_CAP",
    "SignatureHex",
    "V9ConfirmationClaimRequest",
    "V9ConfirmationCompletionReport",
    "V9ConfirmationFailRequest",
    "V9ConfirmationFailResponse",
    "V9ConfirmationJobResponse",
    "V9ConfirmationPrepareRequest",
    "V9ConfirmationPreparedReport",
    "V9ConfirmationRawDimension",
    "V9ConfirmationSubmitRequest",
    "V9ConfirmationSubmitResponse",
]
