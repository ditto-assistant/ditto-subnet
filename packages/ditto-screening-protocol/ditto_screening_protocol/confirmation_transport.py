"""Canonical private Bench v9 confirmation transport models.

Platform serves these models and the validator consumes them.  Keeping their
profiles, field order, and semantic validators in this dependency-light module
means both processes execute the same Python classes instead of merely exposing
similar JSON Schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue

from ditto_screening_protocol.bench_v9 import (
    MIN_CONFIRMATION_BENCH_VERSION,
    V9EvidenceBenchVersion,
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
LONGMEM_SLOT_PATTERN = r"^longmem-[0-3]$"

# Deep-history floors for every confirmation epoch after v9, mirroring
# ``internal/longmemeval/profile.go`` (``V10Min*``). Go validates the profile it
# receives against exactly these numbers, so a profile Python considers valid
# but Go does not is a lease that fails after the ticket is already spent.
LONGMEM_DEEP_HISTORY_FLOOR_BENCH_VERSION = 10
LONGMEM_V10_MIN_CASES_PER_CAPABILITY = 8
LONGMEM_V10_MIN_HISTORY_SESSIONS = 55
LONGMEM_V10_MIN_HISTORY_BYTES = 400_000

LongMemSlotId = Annotated[str, Field(pattern=LONGMEM_SLOT_PATTERN)]

SignatureHex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]+$", min_length=2, max_length=512),
]


class ConfirmationBundleMode(StrEnum):
    """Whether bundle issuance is disabled, measured, or ranking-authoritative."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class ConfirmationEligibilityMode(StrEnum):
    """How base-v9 subjects enter the bounded LongMem confirmation lane."""

    RANK = "rank"
    SCORE_THRESHOLD = "score_threshold"


class ConfirmationProviderLaneProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    lane: Annotated[str, Field(min_length=1, max_length=128)]
    provider: Annotated[str, Field(min_length=1, max_length=128)]
    route_provider: Annotated[str, Field(min_length=1, max_length=128)]
    receipt_provider: Annotated[str, Field(min_length=1, max_length=128)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    max_requests: Annotated[int, Field(ge=1)]
    max_prompt_tokens: Annotated[int, Field(ge=0)]
    max_completion_tokens: Annotated[int, Field(ge=0)]
    max_total_tokens: Annotated[int, Field(ge=0)]
    max_cost_usd_micros: Annotated[int, Field(ge=1)]


class ConfirmationEmbeddingLaneProfile(BaseModel):
    """Frozen retrieval embedding route for the LongMem lane.

    Embeddings are intentionally not a third LongMem judge/reader provider
    receipt.  They have their own purpose-bound Platform capability and budget,
    so a credential minted for retrieval cannot be replayed against either
    chat model.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    lane: Literal["embedding"]
    provider: Annotated[str, Field(min_length=1, max_length=128)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    dimensions: Annotated[int, Field(gt=0, le=16_384)]
    max_requests: Annotated[int, Field(ge=1)]
    max_input_tokens: Annotated[int, Field(ge=1)]
    max_cost_usd_micros: Annotated[int, Field(ge=1)]


class ConfirmationAblationProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    intervention: Literal["inference", "embedding"]
    contract_version: Annotated[str, Field(min_length=1, max_length=128)]
    threshold_micros: Annotated[int, Field(ge=0, le=1_000_000)]
    budget: AblationBudget


class ConfirmationAblationCoordinatorProfile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    sample_size: Annotated[int, Field(gt=0)]
    max_attempts: Literal[1]
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
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: Literal[1]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    formula_revision: Annotated[str, Field(min_length=1, max_length=128)]
    base_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    longmem_weight_bps: Annotated[int, Field(gt=0, lt=10_000)]
    checksum: Sha256


def _without_model_serialization(schema: object) -> object:
    """Copy a core schema with the model node's custom serializer removed."""
    if not isinstance(schema, Mapping):
        return schema
    copied = {key: value for key, value in schema.items() if key != "serialization"}
    if "schema" in copied:
        copied["schema"] = _without_model_serialization(copied["schema"])
    return copied


class ConfirmationExecutionProfile(BaseModel):
    """Complete non-secret profile needed by the trusted local executor."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    schema_version: Literal[1]
    revision: Annotated[str, Field(min_length=1, max_length=128)]
    checksum: Sha256
    # ``bench_version`` and the two deep-history floors below are additive, and
    # are serialized only when they leave their v9 values (see
    # ``_omit_v9_defaults``). Go declares them ``omitempty`` on both the wire
    # struct and the checksummed payload mirror, and recomputes the outer
    # profile checksum from that payload, so emitting ``"bench_version": 9``
    # for a v9 profile would break checksum equality and installed-profile
    # identity against every released executor.
    bench_version: V9EvidenceBenchVersion = 9
    longmem_profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    longmem_profile_checksum: Sha256
    longmem_dataset_revision: Annotated[str, Field(min_length=1, max_length=128)]
    longmem_dataset_sha256: Sha256
    longmem_selector_revision: Literal["longmemeval-s-stratified-sha256-v1"]
    longmem_selection_seed: Annotated[int, Field(ge=0, le=(1 << 64) - 1)]
    longmem_cases_per_capability: Annotated[int, Field(ge=2)]
    longmem_seed_batch_pairs: Annotated[int, Field(gt=0)]
    longmem_min_history_sessions: Annotated[int, Field(ge=0)] = 0
    longmem_min_history_bytes: Annotated[int, Field(ge=0)] = 0
    longmem_projection_key_sha256: Sha256
    provider_lanes: list[ConfirmationProviderLaneProfile]
    embedding_lane: ConfirmationEmbeddingLaneProfile
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

    @model_serializer(mode="wrap")
    def _omit_v9_defaults(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        """Mirror Go's ``omitempty`` on the three additive profile fields.

        A v9 profile must serialize to exactly the bytes it did before these
        fields existed, because Go recomputes the outer profile checksum from
        its own payload marshal and compares the decoded profile to the
        installed one field by field.
        """
        payload = handler(self)
        if self.bench_version == MIN_CONFIRMATION_BENCH_VERSION:
            payload.pop("bench_version", None)
        if not self.longmem_min_history_sessions:
            payload.pop("longmem_min_history_sessions", None)
        if not self.longmem_min_history_bytes:
            payload.pop("longmem_min_history_bytes", None)
        return payload

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Mapping[str, Any], handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Publish the real field set even in serialization mode.

        The ``model_serializer`` above attaches a ``dict[str, Any]`` return
        schema, which collapses every serialization-mode JSON Schema for this
        model -- including the OpenAPI document Backroom's client is generated
        from -- into a free-form object. Dropping the serializer's schema here
        keeps the published contract describing the profile itself; the runtime
        omission of the three v9-default keys is unaffected.
        """
        return handler(_without_model_serialization(core_schema))

    @model_validator(mode="after")
    def longmem_history_floors_match_the_epoch(self) -> ConfirmationExecutionProfile:
        """Reject a profile Go's ``longmemeval.Profile.Validate`` would reject.

        Failing here costs nothing; failing in Go costs a claimed ticket, a
        spent slot, and a hand-back the operator sees only as an opaque
        infrastructure error.
        """
        if self.bench_version < LONGMEM_DEEP_HISTORY_FLOOR_BENCH_VERSION:
            if self.longmem_min_history_sessions or self.longmem_min_history_bytes:
                raise ValueError(
                    "bench_version 9 does not define LongMem deep-history floors"
                )
            return self
        if (
            self.longmem_cases_per_capability < LONGMEM_V10_MIN_CASES_PER_CAPABILITY
            or self.longmem_min_history_sessions < LONGMEM_V10_MIN_HISTORY_SESSIONS
            or self.longmem_min_history_bytes < LONGMEM_V10_MIN_HISTORY_BYTES
        ):
            raise ValueError(
                "confirmation profile is below the LongMem deep-history floor"
            )
        return self

    @model_validator(mode="after")
    def ablation_roles_are_not_swappable(self) -> ConfirmationExecutionProfile:
        lanes = [lane.lane for lane in self.provider_lanes]
        if sorted(lanes) != ["judge", "reader"]:
            raise ValueError("provider_lanes must contain exactly reader and judge")
        if self.inference_ablation.intervention != "inference":
            raise ValueError("inference_ablation must use the inference intervention")
        if self.embedding_ablation.intervention != "embedding":
            raise ValueError("embedding_ablation must use the embedding intervention")
        return self


class V9ConfirmationClaimRequest(BaseModel):
    # FastAPI receives UUIDs and datetimes from JSON as strings.  Field-level
    # constraints still reject malformed values; model-wide strict mode would
    # make this otherwise valid JSON transport impossible to call.
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    slot_id: LongMemSlotId
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    profile_checksum: Sha256
    broker_public_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}=?$")]
    nonce: UUID
    requested_at: datetime
    signature: SignatureHex

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class ConfirmationInferenceGrantOffer(BaseModel):
    """One ticket-scoped, lane-specific Platform inference capability."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    lane: Literal["reader", "judge", "embedding"]
    grant_id: UUID
    bearer: Annotated[str, Field(min_length=32, max_length=128)]
    generation: Annotated[int, Field(ge=1)]
    proxy_url: Annotated[str, Field(min_length=1)]
    model: Annotated[str, Field(min_length=1, max_length=256)]
    provider: Annotated[str, Field(min_length=1, max_length=128)]
    route_provider: Annotated[str, Field(min_length=1, max_length=128)]
    receipt_provider: Annotated[str, Field(min_length=1, max_length=128)]
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)]
    request_budget: Annotated[int, Field(ge=1)]
    token_budget: Annotated[int, Field(ge=1)]
    cost_budget_microusd: Annotated[int, Field(ge=1)]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expires_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @field_validator("proxy_url")
    @classmethod
    def proxy_url_must_be_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname:
            return value
        if parsed.scheme == "http" and parsed.hostname:
            hostname = parsed.hostname
            try:
                is_loopback = ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = hostname == "localhost"
            if is_loopback:
                return value
        raise ValueError("confirmation proxy_url must use HTTPS or loopback HTTP")


class V9ConfirmationJobResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    purpose: Literal["v9_confirmation_bundle"]
    bundle_id: UUID
    ticket_id: UUID
    reservation_id: UUID
    agent_id: UUID
    slot_id: LongMemSlotId
    deadline: datetime
    artifact_sha256: Sha256
    bench_version: V9EvidenceBenchVersion
    settings_revision: Annotated[int, Field(ge=1)]
    settings_checksum: Sha256
    retest_generation: Annotated[int, Field(ge=0)]
    mode: ConfirmationBundleMode
    per_bundle_request_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_REQUEST_CAP)]
    per_bundle_token_cap: Annotated[int, Field(ge=1, le=MAX_BUNDLE_TOKEN_CAP)]
    execution_profile: ConfirmationExecutionProfile
    inference_grants: Annotated[
        list[ConfirmationInferenceGrantOffer], Field(min_length=3, max_length=3)
    ]

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        return value


V9ConfirmationCompletionReport = ConfirmationCompletionReport


class V9ConfirmationSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    ticket_id: UUID
    report: ConfirmationCompletionReport


class V9ConfirmationRawDimension(BaseModel):
    """One exact native Go evidence wrapper returned by the local scorer."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    go_evidence_sha256: Sha256
    latency_ms: UsageCount
    evidence: dict[str, object]


class V9ConfirmationPrepareRequest(BaseModel):
    """Authenticated native Go evidence awaiting Platform normalization."""

    model_config = ConfigDict(extra="ignore")

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
    model_config = ConfigDict(extra="ignore", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    ablation_coordinator_latency_ms: UsageCount
    longmemeval: LongMemDimensionEnvelope
    inference_ablation: AblationDimensionEnvelope
    embedding_ablation: AblationDimensionEnvelope
    evidence_sha256: Sha256


# Allowlisted diagnostic classes a reporter may attach to a hand-back. The
# protocol reason (four values) drives reissue policy; this says WHY without
# ever becoming an error-string channel. Anything a reporter cannot classify
# arrives as "unclassified" rather than as an arbitrary type or message. Both
# sides read this one list so a validator can never widen it unilaterally.
CONFIRMATION_FAILURE_CLASS_VALUES = (
    "sandbox_oom",
    "lease_revoked",
    "validator_infrastructure",
    "platform_infrastructure",
    "dittobench",
    "platform",
    "validator",
    "evidence_schema",
    "timeout",
    "transport",
    "unclassified",
)

ConfirmationFailureClass = Literal[
    "sandbox_oom",
    "lease_revoked",
    "validator_infrastructure",
    "platform_infrastructure",
    "dittobench",
    "platform",
    "validator",
    "evidence_schema",
    "timeout",
    "transport",
    "unclassified",
]

# The last progress stage the slot published before it broke, plus "unknown"
# for a slot that failed before publishing any stage at all.
ConfirmationFailureStage = Literal[
    "preparing",
    "running_confirmation",
    "finalizing",
    "submitting_result",
    "failed_retrying",
    "unknown",
]


class V9ConfirmationFailRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=SS58_PATTERN)]
    ticket_id: UUID
    reason: Literal["execution_failed", "deadline", "cancelled", "infrastructure"]
    # Optional so a reporter predating this contract keeps handing work back
    # unchanged: it signs the v1 message and Platform verifies v1. A reporter
    # that sends a class signs the v2 message, which binds both diagnostics, so
    # neither field can be forged or stripped in flight. New reporter against
    # old Platform fails its signature check — deploy Platform first.
    failure_class: ConfirmationFailureClass | None = None
    failure_stage: ConfirmationFailureStage | None = None
    nonce: UUID
    requested_at: datetime
    signature: SignatureHex

    @model_validator(mode="after")
    def _diagnostics_travel_together(self) -> V9ConfirmationFailRequest:
        if (self.failure_class is None) != (self.failure_stage is None):
            raise ValueError("failure class and stage must be reported together")
        return self

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class V9ConfirmationFailResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    bundle_id: UUID
    ticket_id: UUID
    accepted: Literal[True]
    state: Literal["failed"]
    settled_microusd: Annotated[int, Field(ge=0)]
    replayed: bool


class V9ConfirmationSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

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
    "ConfirmationEligibilityMode",
    "ConfirmationCompositeProfile",
    "ConfirmationEmbeddingLaneProfile",
    "ConfirmationExecutionProfile",
    "ConfirmationInferenceGrantOffer",
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
