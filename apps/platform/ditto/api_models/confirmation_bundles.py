"""Wire contracts for bounded LongMem confirmation bundles.

The expensive dimensions are one indivisible bundle.  These models keep the
operator policy, evidence provenance, and base/full ranking state explicit so a
consumer cannot accidentally compare a provisional base score with a confirmed
full score.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ditto_screening_protocol.bench_v9 import (
    CONFIRMATION_BENCH_VERSIONS as CONFIRMATION_BENCH_VERSIONS,
)
from ditto_screening_protocol.bench_v9 import (
    MIN_CONFIRMATION_BENCH_VERSION as MIN_CONFIRMATION_BENCH_VERSION,
)
from ditto_screening_protocol.bench_v9 import (
    supports_confirmation as supports_confirmation,
)
from ditto_screening_protocol.confirmation import (
    AblationBudget as AblationBudget,
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
    ConfirmationCompositePolicy as ConfirmationCompositePolicy,
)
from ditto_screening_protocol.confirmation import (
    ConfirmationEvidenceRoot as ConfirmationEvidenceRoot,
)
from ditto_screening_protocol.confirmation import (
    ConfirmationUsageTotals as ConfirmationUsageTotals,
)
from ditto_screening_protocol.confirmation import (
    FactorBPS,
    ScoreMicros,
    UsageCount,
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
    ConfirmationBundleMode,
    ConfirmationEligibilityMode,
)

DEFAULT_CONFIRMATION_TOP_N = 5
MAX_CONFIRMATION_TOP_N = 10
DEFAULT_CONFIRMATION_MIN_BASE_SCORE_MICROS = 950_000
MAX_DAILY_BUNDLE_CAP = 1_000
MAX_DAILY_DOLLAR_MICROUSD = 1_000_000_000


def confirmation_capable(column: Any) -> Any:
    """SQL form of :func:`supports_confirmation` for a bench-version column.

    The ranking queries have to express the same rule in SQLAlchemy. Deriving
    the ``IN`` list from the one alias keeps a query branch from silently
    disagreeing with the in-process predicate -- the exact drift that let the
    enforce-mode ledger keep gating bench 9 alone.
    """
    return column.in_(CONFIRMATION_BENCH_VERSIONS)


Sha256 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]


class ConfirmationBundleState(StrEnum):
    """Durable evidence lifecycle, separate from ordinary validator tickets."""

    BLOCKED_BUDGET = "blocked_budget"
    PENDING = "pending"
    LEASED = "leased"
    FAILED = "failed"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class ConfirmationResultStatus(StrEnum):
    """Which score contract an agent row currently satisfies."""

    BASE_ONLY = "base_only"
    PROVISIONAL = "provisional"
    FULL_CONFIRMED = "full_confirmed"


class ConfirmationDimension(StrEnum):
    LONGMEMEVAL = "longmemeval"
    INFERENCE_ABLATION = "inference_ablation"
    EMBEDDING_ABLATION = "embedding_ablation"


class ConfirmationReservationState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"


class ConfirmationBundleSettings(BaseModel):
    """Complete audited policy for issuing one bounded confirmation bundle.

    The shipped default is intentionally unconfigured and off.  No profile or
    checksum is guessed in source.  Shadow/enforce revisions must provide every
    cap and a frozen profile identity before the API accepts them.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: ConfirmationBundleMode = ConfirmationBundleMode.OFF
    eligibility_mode: ConfirmationEligibilityMode = ConfirmationEligibilityMode.RANK
    top_n: Annotated[int, Field(ge=1, le=MAX_CONFIRMATION_TOP_N)] = (
        DEFAULT_CONFIRMATION_TOP_N
    )
    min_base_score_micros: Annotated[int, Field(ge=0, le=1_000_000)] = (
        DEFAULT_CONFIRMATION_MIN_BASE_SCORE_MICROS
    )
    daily_bundle_cap: Annotated[int, Field(ge=0, le=MAX_DAILY_BUNDLE_CAP)] = 0
    daily_dollar_cap_microusd: Annotated[
        int, Field(ge=0, le=MAX_DAILY_DOLLAR_MICROUSD)
    ] = 0
    per_bundle_request_cap: Annotated[int, Field(ge=0, le=MAX_BUNDLE_REQUEST_CAP)] = 0
    per_bundle_token_cap: Annotated[int, Field(ge=0, le=MAX_BUNDLE_TOKEN_CAP)] = 0
    profile_revision: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    profile_checksum: Sha256 | None = None
    challenger_z: Annotated[float, Field(ge=0.0, le=3.0)] = 1.64

    @field_validator("mode", mode="before")
    @classmethod
    def _parse_wire_mode(cls, value: object) -> object:
        # FastAPI hands Pydantic an already-decoded dict, so strict mode would
        # otherwise demand a Python enum instance that no JSON client can send.
        return ConfirmationBundleMode(value) if isinstance(value, str) else value

    @field_validator("eligibility_mode", mode="before")
    @classmethod
    def _parse_wire_eligibility_mode(cls, value: object) -> object:
        return ConfirmationEligibilityMode(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _require_complete_active_policy(self) -> ConfirmationBundleSettings:
        if (self.profile_revision is None) != (self.profile_checksum is None):
            raise ValueError(
                "profile_revision and profile_checksum must be configured together"
            )
        if self.mode != ConfirmationBundleMode.OFF:
            missing = [
                name
                for name, value in (
                    ("daily_bundle_cap", self.daily_bundle_cap),
                    ("daily_dollar_cap_microusd", self.daily_dollar_cap_microusd),
                    ("per_bundle_request_cap", self.per_bundle_request_cap),
                    ("per_bundle_token_cap", self.per_bundle_token_cap),
                )
                if value == 0
            ]
            if self.profile_revision is None:
                missing.append("confirmation_profile")
            if missing:
                raise ValueError(
                    "shadow/enforce confirmation policy is incomplete: "
                    + ", ".join(missing)
                )
        return self


class ConfirmationBundleSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ConfirmationBundleSettings
    checksum: Sha256
    reason: str
    actor: str
    created_at: datetime


class EffectiveConfirmationBundleSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: ConfirmationBundleSettings
    checksum: Sha256 | None
    source: Literal["default", "revision"]
    configured: bool
    issuance_active: bool
    max_top_n: int = MAX_CONFIRMATION_TOP_N
    max_daily_bundle_cap: int = MAX_DAILY_BUNDLE_CAP
    max_daily_dollar_microusd: int = MAX_DAILY_DOLLAR_MICROUSD
    max_bundle_request_cap: int = MAX_BUNDLE_REQUEST_CAP
    max_bundle_token_cap: int = MAX_BUNDLE_TOKEN_CAP


class AdminConfirmationBundleSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ConfirmationBundleSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminConfirmationBundleSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[ConfirmationBundleSettingsRevision]
    history: list[ConfirmationBundleSettingsRevision]
    default: ConfirmationBundleSettings
    effective: EffectiveConfirmationBundleSettings


class ConfirmationDimensionEvidenceView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    dimension: ConfirmationDimension
    status: Literal["completed", "not_run", "unavailable"]
    evidence_sha256: Sha256
    request_count: UsageCount
    input_tokens: UsageCount
    output_tokens: UsageCount
    provider_cost_microusd: UsageCount
    latency_ms: UsageCount
    synthetic: bool
    evidence: LongMemEvidence | AblationEvidence
    created_at: datetime


PrepareRejectionCode = Literal[
    "go_evidence_digest_mismatch",
    "go_evidence_fields_drifted",
    "unsupported_ablation_status",
    "unsupported_ablation_contract",
    "ablation_profile_drift",
    "ablation_accounting",
    "ablation_digest_mismatch",
    "longmem_profile_drift",
    "longmem_accounting",
    "longmem_digest_mismatch",
    "longmem_latency_drift",
    "unsupported_bench_version",
    "confirmation_wire",
    "confirmation_evidence",
    "unclassified",
]
PREPARE_REJECTION_CODES: tuple[PrepareRejectionCode, ...] = (
    "go_evidence_digest_mismatch",
    "go_evidence_fields_drifted",
    "unsupported_ablation_status",
    "unsupported_ablation_contract",
    "ablation_profile_drift",
    "ablation_accounting",
    "ablation_digest_mismatch",
    "longmem_profile_drift",
    "longmem_accounting",
    "longmem_digest_mismatch",
    "longmem_latency_drift",
    "unsupported_bench_version",
    "confirmation_wire",
    "confirmation_evidence",
    "unclassified",
)


class ConfirmationBundleTicketView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ticket_id: UUID
    validator_hotkey: str
    slot_id: str
    status: Literal["issued", "scored", "expired"]
    attempt: Annotated[int, Field(ge=1)]
    issued_at: datetime
    deadline: datetime
    failure_reason: str | None
    # The signed, allowlisted diagnostics behind ``failure_reason``. Null for a
    # reporter predating the contract, so a consumer must not require them.
    failure_class: str | None = None
    failure_stage: str | None = None
    failed_at: datetime | None
    # Allowlisted Go→Python prepare-report rejection. Written when convert or
    # rebuild 409s; null when prepare never ran or succeeded. Not an error
    # string, and it does not change retry ownership or settlement.
    prepare_rejection: PrepareRejectionCode | None = None
    prepare_rejected_at: datetime | None = None


class ConfirmationBundleSubjectView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    bench_version: int
    artifact_sha256: Sha256
    result_status: ConfirmationResultStatus
    base_evidence_sha256: Sha256
    base_quality_micros: ScoreMicros
    base_stderr_micros: ScoreMicros
    base_model_factor_bps: FactorBPS
    base_tool_factor_bps: FactorBPS
    full_quality_micros: ScoreMicros | None
    full_stderr_micros: ScoreMicros | None
    semantic_factor_bps: FactorBPS | None
    applied_factor_bps: FactorBPS | None
    full_effective_micros: ScoreMicros | None
    bundle_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ConfirmationBundleView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    bundle_id: UUID
    artifact_sha256: Sha256
    bench_version: int
    profile_revision: str
    profile_checksum: Sha256
    retest_generation: int
    generation_reason: Literal["initial", "operator_retest", "settings_supersession"]
    source_bundle_id: UUID | None
    state: ConfirmationBundleState
    settings_revision: int
    settings_checksum: Sha256
    qualification_status: Literal["qualified", "unqualified"] | None
    completion_mode: ConfirmationBundleMode | None
    completion_ticket_id: UUID | None
    evidence_sha256: Sha256 | None
    reporter_hotkey: str | None
    bundle_signature: str | None
    evidence_root: ConfirmationEvidenceRoot | None
    verified_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    subjects: list[ConfirmationBundleSubjectView] = Field(default_factory=list)
    dimensions: list[ConfirmationDimensionEvidenceView] = Field(default_factory=list)
    tickets: list[ConfirmationBundleTicketView] = Field(default_factory=list)


class ConfirmationDailyBudgetView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    utc_day: date
    revision: int
    issued_attempts: int
    outstanding_reserved_microusd: int
    settled_microusd: int

    @property
    def committed_microusd(self) -> int:
        return self.outstanding_reserved_microusd + self.settled_microusd


class ConfirmationShadowCalibrationView(BaseModel):
    """Measured shadow economics derived only from settled Platform rows.

    The daily projection is the observed cost run-rate across the inclusive
    interval between the first and last settled sample.  Epoch spend remains
    unavailable until an epoch duration is explicitly configured; the API does
    not guess a chain constant.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    observed_from_utc_day: date | None
    observed_through_utc_day: date | None
    observation_days: Annotated[int, Field(ge=0)]
    confirmation_profile_revision: str | None
    confirmation_profile_checksum: Sha256 | None
    base_run_count: Annotated[int, Field(ge=0)]
    measured_base_cost_microusd: Annotated[int, Field(ge=0)] | None
    confirmation_bundle_count: Annotated[int, Field(ge=0)]
    measured_bundle_cost_microusd: Annotated[int, Field(ge=0)] | None
    bench_version: Annotated[int, Field(ge=1)]
    # ``completed`` is evidence actually produced. A generation that was
    # superseded by an operator settings write, or that failed every attempt,
    # is counted on its own axis so an execution outage cannot be read as a
    # completed cohort that simply never promoted.
    completed_bundle_count: Annotated[int, Field(ge=0)]
    superseded_bundle_count: Annotated[int, Field(ge=0)]
    failed_bundle_count: Annotated[int, Field(ge=0)]
    qualified_bundle_count: Annotated[int, Field(ge=0)]
    promotion_rate_bps: FactorBPS | None
    projected_daily_spend_microusd: Annotated[int, Field(ge=0)] | None
    epoch_duration_seconds: Annotated[int, Field(gt=0)] | None
    projected_epoch_spend_microusd: Annotated[int, Field(ge=0)] | None
    epoch_projection_unavailable_reason: str | None

    @model_validator(mode="after")
    def _validate_projection_availability(self) -> ConfirmationShadowCalibrationView:
        if (self.confirmation_profile_revision is None) != (
            self.confirmation_profile_checksum is None
        ):
            raise ValueError("confirmation profile identity must be available together")
        if self.qualified_bundle_count > self.completed_bundle_count:
            raise ValueError(
                "qualified_bundle_count cannot exceed completed_bundle_count"
            )
        if (self.base_run_count == 0) != (self.measured_base_cost_microusd is None):
            raise ValueError("base cost availability must match its sample count")
        if (self.confirmation_bundle_count == 0) != (
            self.measured_bundle_cost_microusd is None
        ):
            raise ValueError("bundle cost availability must match its sample count")
        if (self.completed_bundle_count == 0) != (self.promotion_rate_bps is None):
            raise ValueError("promotion rate availability must match its sample count")
        if self.observation_days == 0:
            if (
                self.observed_from_utc_day is not None
                or self.observed_through_utc_day is not None
                or self.projected_daily_spend_microusd is not None
            ):
                raise ValueError(
                    "an empty observation window cannot publish a daily projection"
                )
        elif (
            self.observed_from_utc_day is None
            or self.observed_through_utc_day is None
            or self.projected_daily_spend_microusd is None
        ):
            raise ValueError(
                "a non-empty observation window requires a daily projection"
            )
        elif (
            self.observation_days
            != (self.observed_through_utc_day - self.observed_from_utc_day).days + 1
        ):
            raise ValueError(
                "observation_days must match the inclusive UTC date window"
            )
        epoch_available = self.epoch_duration_seconds is not None
        if epoch_available != (self.projected_epoch_spend_microusd is not None):
            raise ValueError("epoch duration and projection must be available together")
        if epoch_available == (self.epoch_projection_unavailable_reason is not None):
            raise ValueError(
                "epoch projection must publish exactly one availability state"
            )
        return self


class AdminConfirmationBundleListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[ConfirmationBundleView]
    count: int
    budget: ConfirmationDailyBudgetView
    shadow_calibration: ConfirmationShadowCalibrationView


class AdminConfirmationBundleRetestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    request_id: Annotated[UUID, Field(strict=False)]
    expected_generation: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminConfirmationBundleRetestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    authorization_id: UUID
    superseded_bundle_id: UUID
    bundle: ConfirmationBundleView
    replayed: bool


__all__ = [
    "AblationBudget",
    "AblationDimensionEnvelope",
    "AblationEvidence",
    "AblationSyntheticUsage",
    "AdminConfirmationBundleListResponse",
    "AdminConfirmationBundleRetestRequest",
    "AdminConfirmationBundleRetestResponse",
    "AdminConfirmationBundleSettingsRequest",
    "AdminConfirmationBundleSettingsResponse",
    "ConfirmationBundleMode",
    "ConfirmationBundleSettings",
    "ConfirmationBundleSettingsRevision",
    "ConfirmationBundleState",
    "ConfirmationBundleSubjectView",
    "ConfirmationBundleTicketView",
    "ConfirmationBundleView",
    "ConfirmationCompositePolicy",
    "ConfirmationCompletionReport",
    "ConfirmationDailyBudgetView",
    "ConfirmationDimension",
    "ConfirmationDimensionEvidenceView",
    "ConfirmationEvidenceRoot",
    "PREPARE_REJECTION_CODES",
    "PrepareRejectionCode",
    "ConfirmationReservationState",
    "ConfirmationResultStatus",
    "ConfirmationShadowCalibrationView",
    "DEFAULT_CONFIRMATION_TOP_N",
    "EffectiveConfirmationBundleSettings",
    "LongMemCapabilityScore",
    "LongMemDimensionEnvelope",
    "LongMemEvidence",
    "LongMemProviderLaneEvidence",
    "LongMemScoreEvidence",
    "MAX_BUNDLE_REQUEST_CAP",
    "MAX_BUNDLE_TOKEN_CAP",
    "MAX_CONFIRMATION_TOP_N",
    "MAX_DAILY_BUNDLE_CAP",
    "MAX_DAILY_DOLLAR_MICROUSD",
]
