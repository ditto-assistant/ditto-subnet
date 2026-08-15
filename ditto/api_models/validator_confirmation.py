"""Shared confirmation transport exports plus validator-local scorer models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ditto_screening_protocol.confirmation import (
    AblationBudget,
    Sha256,
)
from ditto_screening_protocol.confirmation import (
    AblationDimensionEnvelope as AblationDimensionEnvelope,
)
from ditto_screening_protocol.confirmation import (
    AblationEvidence as AblationEvidence,
)
from ditto_screening_protocol.confirmation import (
    AblationSyntheticUsage as AblationSyntheticUsage,
)
from ditto_screening_protocol.confirmation import (
    ConfirmationCompletionReport as ConfirmationCompletionReport,
)
from ditto_screening_protocol.confirmation import (
    ConfirmationUsageTotals as ConfirmationUsageTotals,
)
from ditto_screening_protocol.confirmation import (
    LongMemCapability as LongMemCapability,
)
from ditto_screening_protocol.confirmation import (
    LongMemCapabilityScore as LongMemCapabilityScore,
)
from ditto_screening_protocol.confirmation import (
    LongMemDimensionEnvelope as LongMemDimensionEnvelope,
)
from ditto_screening_protocol.confirmation import (
    LongMemEvidence as LongMemEvidence,
)
from ditto_screening_protocol.confirmation import (
    LongMemProviderLaneEvidence as LongMemProviderLaneEvidence,
)
from ditto_screening_protocol.confirmation import (
    LongMemScoreEvidence as LongMemScoreEvidence,
)
from ditto_screening_protocol.confirmation_transport import (
    MAX_BUNDLE_REQUEST_CAP,
    MAX_BUNDLE_TOKEN_CAP,
    ConfirmationAblationCoordinatorProfile,
    ConfirmationAblationProfile,
    ConfirmationBundleMode,
    ConfirmationCompositeProfile,
    ConfirmationEmbeddingLaneProfile,
    ConfirmationExecutionProfile,
    ConfirmationInferenceGrantOffer,
    ConfirmationProviderLaneProfile,
    LongMemSlotId,
    V9ConfirmationClaimRequest,
    V9ConfirmationCompletionReport,
    V9ConfirmationFailRequest,
    V9ConfirmationFailResponse,
    V9ConfirmationJobResponse,
    V9ConfirmationPreparedReport,
    V9ConfirmationPrepareRequest,
    V9ConfirmationRawDimension,
    V9ConfirmationSubmitRequest,
    V9ConfirmationSubmitResponse,
)


class V9ConfirmationScorerReadiness(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ready: Literal[True]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    profile_checksum: Sha256


class V9ConfirmationScorerRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    purpose: Literal["v9_confirmation_bundle"]
    bundle_id: UUID
    ticket_id: UUID
    agent_id: UUID
    slot_id: LongMemSlotId
    inference_session_id: Annotated[str, Field(min_length=16, max_length=128)]
    artifact_url: Annotated[str, Field(min_length=1)]
    artifact_sha256: Sha256
    screened_image_url: Annotated[str, Field(min_length=1)]
    screened_image_sha256: Sha256
    screened_image_size_bytes: Annotated[int, Field(gt=0)]
    screened_image_id: Annotated[str, Field(min_length=1)]
    screened_image_ref: Annotated[str, Field(min_length=1)]
    bench_version: Literal[9]
    deadline: datetime
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    profile_checksum: Sha256
    settings_revision: Annotated[int, Field(ge=1)]
    settings_checksum: Sha256
    retest_generation: Annotated[int, Field(ge=0)]
    mode: Literal["shadow", "enforce"]
    per_bundle_request_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_REQUEST_CAP)]
    per_bundle_token_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_TOKEN_CAP)]
    execution_profile: ConfirmationExecutionProfile

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        return value


class V9ConfirmationScorerResult(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    longmemeval: V9ConfirmationRawDimension
    inference_ablation: V9ConfirmationRawDimension
    embedding_ablation: V9ConfirmationRawDimension
    ablation_coordinator_latency_ms: Annotated[int, Field(gt=0)]
    evidence_sha256: Sha256


__all__ = [
    "AblationBudget",
    "ConfirmationAblationCoordinatorProfile",
    "ConfirmationAblationProfile",
    "ConfirmationBundleMode",
    "ConfirmationCompositeProfile",
    "ConfirmationEmbeddingLaneProfile",
    "ConfirmationExecutionProfile",
    "ConfirmationInferenceGrantOffer",
    "ConfirmationProviderLaneProfile",
    "V9ConfirmationClaimRequest",
    "V9ConfirmationCompletionReport",
    "V9ConfirmationFailRequest",
    "V9ConfirmationFailResponse",
    "V9ConfirmationJobResponse",
    "V9ConfirmationPrepareRequest",
    "V9ConfirmationPreparedReport",
    "V9ConfirmationRawDimension",
    "V9ConfirmationScorerReadiness",
    "V9ConfirmationScorerRequest",
    "V9ConfirmationScorerResult",
    "V9ConfirmationSubmitRequest",
    "V9ConfirmationSubmitResponse",
]
