"""Versioned operator settings for private L2/L3 source review."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto_screening_protocol import SCREENING_POLICY_VERSION

ReviewMode = Literal["off", "shadow", "enforce", "inherit"]
ReviewModel = Literal[
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "openai/gpt-5.6-sol",
]
ReasoningEffort = Literal["low", "medium", "high"]
SourceReviewModel = Literal["openai/gpt-5.6-luna"]
PolicyManifestProfile = Literal["core", "l1", "l1_l2"]

_POLICY_MANIFEST_MODULES: dict[PolicyManifestProfile, list[dict[str, str]]] = {
    "core": [],
    "l1": [
        {"kind": "agentic_source_review", "id": "luna-source-review"},
        {"kind": "behavioral_oracle", "id": "v8-behavioral-oracle"},
    ],
    "l1_l2": [
        {"kind": "agentic_source_review", "id": "luna-sol-source-review"},
        {"kind": "behavioral_oracle", "id": "v8-behavioral-oracle"},
    ],
}


def policy_manifest_digest(profile: PolicyManifestProfile, rotation_id: str) -> str:
    payload = {
        "policy_version": SCREENING_POLICY_VERSION,
        "rotation_id": rotation_id,
        "modules": _POLICY_MANIFEST_MODULES[profile],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ScreenerReviewSettings(BaseModel):
    """Strict, secret-free settings applied between screening leases."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: ReviewMode = "off"
    l2_model: ReviewModel = "moonshotai/kimi-k3"
    l2_fallback_models: tuple[ReviewModel, ...] = (
        "z-ai/glm-5.2",
        "openai/gpt-5.6-sol",
    )
    l3_enabled: bool = True
    l3_model: Literal["openai/gpt-5.6-sol"] = "openai/gpt-5.6-sol"
    timeout_seconds: Annotated[int, Field(ge=30, le=1_800)] = 1_200
    max_steps: Annotated[int, Field(ge=1, le=48)] = 32
    # L1 Luna budget. Distinct from ``max_steps``, which bounds L2. Exhausting
    # either L1 bound yields ``pass_inconclusive`` and admits the artifact.
    source_review_max_steps: Annotated[int, Field(ge=1, le=240)] = 200
    source_review_max_read_bytes: Annotated[int, Field(ge=32_000, le=16_000_000)] = (
        8_000_000
    )
    source_review_reasoning_effort: Literal["low", "medium", "high"] = "high"
    source_review_model: SourceReviewModel = "openai/gpt-5.6-luna"
    source_review_timeout_seconds: Annotated[int, Field(ge=60, le=3_600)] = 3_600
    max_input_tokens: Annotated[int, Field(ge=1, le=1_000_000)] = 425_000
    max_output_tokens: Annotated[int, Field(ge=1, le=128_000)] = 20_000
    max_completion_tokens: Annotated[int, Field(ge=1, le=128_000)] = 2_400
    max_cost_usd: Annotated[float, Field(gt=0, le=10)] = 6.0
    critic_reasoning_effort: ReasoningEffort = "medium"
    # Gradient thresholds for a budget-terminated review's notes ledger: this
    # many recorded concerns hold the artifact for operator review with the
    # notes attached; zero concerns plus this many cleared notes admit it on
    # positive coverage. Step/time budgets tune inspection depth, not fate.
    concern_hold_count: Annotated[int, Field(ge=1, le=16)] = 1
    clear_min_notes: Annotated[int, Field(ge=1, le=32)] = 3
    cache_ttl_seconds: Annotated[int, Field(ge=60, le=2_592_000)] = 604_800
    audit_retention_days: Annotated[int, Field(ge=1, le=365)] = 30
    policy_manifest_profile: PolicyManifestProfile = "l1"
    policy_manifest_rotation_id: Annotated[
        str, Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    ] = "v8-luna-source-review-behavioral-oracle"

    @model_validator(mode="before")
    @classmethod
    def default_manifest_profile_for_legacy_revision(cls, value: object) -> object:
        if isinstance(value, dict) and "policy_manifest_profile" not in value:
            value = dict(value)
            value["policy_manifest_profile"] = (
                "l1_l2" if value.get("mode") == "enforce" else "l1"
            )
        if isinstance(value, dict) and "policy_manifest_rotation_id" not in value:
            value = dict(value)
            value["policy_manifest_rotation_id"] = (
                "v8-luna-sol-l2-source-review-behavioral-oracle"
                if value.get("mode") == "enforce"
                else "v8-luna-source-review-behavioral-oracle"
            )
        return value

    @field_validator("l2_fallback_models", mode="before")
    @classmethod
    def accept_json_model_chain(cls, value: object) -> object:
        """Preserve an immutable chain after FastAPI decodes its JSON array."""
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_model_chain(self) -> ScreenerReviewSettings:
        chain = (self.l2_model, *self.l2_fallback_models)
        if len(chain) != len(set(chain)):
            raise ValueError("L2 model chain must not contain duplicates")
        if self.max_completion_tokens > self.max_output_tokens:
            raise ValueError("completion budget must not exceed output budget")
        return self


class ScreenerReviewSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ScreenerReviewSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveScreenerReviewSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: ScreenerReviewSettings
    checksum: str
    max_age_seconds: int = 60


class AdminScreenerReviewSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ScreenerReviewSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AppliedScreenerReviewSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    instance_id: str
    revision: int
    scope: str
    mode: ReviewMode
    checksum: str
    source: Literal["platform", "cache", "bootstrap"]
    seen_at: datetime
    fresh: bool
    matches_effective: bool
    expected_revision: int
    expected_scope: str
    expected_checksum: str
    policy_manifest_profile: PolicyManifestProfile
    policy_manifest_rotation_id: str
    policy_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expected_policy_manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AdminShadowReviewObservation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    attempt_id: UUID
    agent_id: UUID
    settings_revision: int
    settings_scope: str
    settings_checksum: str
    disposition: Literal["safe", "violation", "inconclusive", "retryable_infra"]
    risk_level: Literal["low", "medium", "high"] | None
    categories: list[str]
    finding_digest: str | None
    resolution_basis: str | None
    clearance_path: str | None
    critic_disposition: str | None
    adjudicator_disposition: str | None
    response_models: list[str]
    response_providers: list[str]
    usage: dict[str, int | float | None]
    created_at: datetime


class ScreenerPolicyManifestView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    policy_version: int
    profile: PolicyManifestProfile
    rotation_id: str
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reason: str
    actor: str
    created_at: datetime


class AdminScreenerReviewSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[ScreenerReviewSettingsRevision]
    history: list[ScreenerReviewSettingsRevision]
    known_instances: list[str]
    applied_instances: list[AppliedScreenerReviewSettings]
    shadow_observations: list[AdminShadowReviewObservation]
    policy_manifests: list[ScreenerPolicyManifestView]
