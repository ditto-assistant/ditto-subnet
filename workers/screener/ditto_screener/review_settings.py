"""Strict platform-managed L2/L3 settings and last-valid local cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto_screener.config import ScreenerConfig

ReviewModel = Literal[
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "openai/gpt-5.6-sol",
]
SourceReviewModel = Literal["openai/gpt-5.6-luna"]

_MAX_SHADOW_PROVIDER_STAGES = 50


class ShadowReviewUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cached_input_tokens: Annotated[int, Field(ge=0)]
    reasoning_tokens: Annotated[int, Field(ge=0)]
    estimated_cost_usd: Annotated[float, Field(ge=0, le=25)]
    reported_cost_usd: Annotated[float, Field(ge=0, le=25)] | None = None


class ShadowReviewObservationRequest(BaseModel):
    """Bounded non-authoritative telemetry for one active shadow attempt."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    attempt_id: UUID
    artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    settings_revision: Annotated[int, Field(ge=1)]
    settings_scope: Annotated[str, Field(pattern=r"^(?:\*|[a-zA-Z0-9._-]{1,63})$")]
    settings_checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    disposition: Literal["safe", "violation", "inconclusive", "retryable_infra"]
    risk_level: Literal["low", "medium", "high"] | None = None
    categories: tuple[Annotated[str, Field(max_length=64)], ...] = ()
    finding_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    resolution_basis: Annotated[str, Field(max_length=80)] | None = None
    clearance_path: Annotated[str, Field(max_length=100)] | None = None
    critic_disposition: Annotated[str, Field(max_length=80)] | None = None
    adjudicator_disposition: Annotated[str, Field(max_length=80)] | None = None
    response_models: tuple[Annotated[str, Field(max_length=100)], ...] = ()
    response_providers: tuple[Annotated[str, Field(max_length=100)], ...] = ()
    usage: ShadowReviewUsage

    @model_validator(mode="after")
    def validate_bounds(self) -> ShadowReviewObservationRequest:
        if len(self.categories) > 8:
            raise ValueError("shadow review has too many categories")
        if (
            len(self.response_models) > _MAX_SHADOW_PROVIDER_STAGES
            or len(self.response_providers) > _MAX_SHADOW_PROVIDER_STAGES
        ):
            raise ValueError("shadow review has too many provider stages")
        if self.disposition in {"safe", "violation"} and self.risk_level is None:
            raise ValueError("decisive shadow review requires a risk level")
        return self


class ShadowReviewObservationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    accepted: bool


class ReviewSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: Literal["off", "shadow", "enforce"]
    l2_model: ReviewModel
    l2_fallback_models: tuple[ReviewModel, ...]
    l3_enabled: bool = True
    l3_model: Literal["openai/gpt-5.6-sol"]
    timeout_seconds: Annotated[int, Field(ge=30, le=900)]
    max_steps: Annotated[int, Field(ge=1, le=20)]
    source_review_max_steps: Annotated[int, Field(ge=1, le=240)] = 200
    source_review_max_read_bytes: Annotated[int, Field(ge=32_000, le=16_000_000)] = (
        8_000_000
    )
    source_review_reasoning_effort: Literal["low", "medium", "high"] = "high"
    source_review_model: SourceReviewModel = "openai/gpt-5.6-luna"
    source_review_timeout_seconds: Annotated[int, Field(ge=60, le=3_600)] = 1_800
    max_input_tokens: Annotated[int, Field(ge=1, le=1_000_000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=128_000)]
    max_completion_tokens: Annotated[int, Field(ge=1, le=128_000)]
    max_cost_usd: Annotated[float, Field(gt=0, le=10)]
    critic_reasoning_effort: Literal["low", "medium"]
    cache_ttl_seconds: Annotated[int, Field(ge=60, le=2_592_000)]
    audit_retention_days: Annotated[int, Field(ge=1, le=365)]
    policy_manifest_profile: Literal["core", "l1", "l1_l2"] = "l1"
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
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def validate_chain(self) -> ReviewSettings:
        chain = (self.l2_model, *self.l2_fallback_models)
        if len(chain) != len(set(chain)):
            raise ValueError("L2 model chain must not contain duplicates")
        if self.max_completion_tokens > self.max_output_tokens:
            raise ValueError("completion budget must not exceed output budget")
        return self


# Ordered oldest-first by the revision in which each control was introduced.
# Checksums minted before a given entry omit it and every later entry.
_POST_CHECKSUM_FIELDS: tuple[str, ...] = (
    "l3_enabled",
    "source_review_max_steps",
    "source_review_max_read_bytes",
    "source_review_reasoning_effort",
    "source_review_model",
    "source_review_timeout_seconds",
)
_DEFAULTS = {
    name: ReviewSettings.model_fields[name].default for name in _POST_CHECKSUM_FIELDS
}


class EffectiveReviewSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    revision: Annotated[int, Field(ge=0)]
    scope: str
    settings: ReviewSettings
    checksum: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    max_age_seconds: Annotated[int, Field(ge=1, le=3600)]

    @model_validator(mode="after")
    def validate_checksum(self) -> EffectiveReviewSettings:
        current = self.settings.model_dump(mode="json")
        payload = json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(payload).hexdigest() == self.checksum:
            return self

        # A revision minted before a control existed cannot carry that key in
        # the canonical JSON its immutable checksum was taken over, so replay
        # the payload as it stood when each field was introduced, newest first.
        # A field is only droppable while it still holds its default.
        def matches_legacy(legacy: dict[str, object]) -> bool:
            for name in reversed(_POST_CHECKSUM_FIELDS):
                if getattr(self.settings, name) != _DEFAULTS[name]:
                    break
                legacy.pop(name, None)
                candidate = json.dumps(
                    legacy, sort_keys=True, separators=(",", ":")
                ).encode()
                if hashlib.sha256(candidate).hexdigest() == self.checksum:
                    return True
            return False

        if matches_legacy(dict(current)):
            return self
        # Manifest selectors postdate every legacy revision. Their inferred
        # values intentionally depend on mode, so they cannot use the static
        # default-only replay below.
        legacy = dict(current)
        legacy.pop("policy_manifest_profile", None)
        legacy.pop("policy_manifest_rotation_id", None)
        candidate = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(candidate).hexdigest() == self.checksum:
            return self
        if matches_legacy(legacy):
            return self
        raise ValueError("review settings checksum mismatch")

    def apply_to(self, config: ScreenerConfig) -> ScreenerConfig:
        value = self.settings
        return replace(
            config,
            l2_review_mode=value.mode,
            l2_review_model=value.l2_model,
            l2_fallback_models=value.l2_fallback_models,
            l3_review_enabled=value.l3_enabled,
            l3_review_model=value.l3_model,
            l2_timeout_seconds=float(value.timeout_seconds),
            l2_max_steps=value.max_steps,
            source_review_max_steps=value.source_review_max_steps,
            source_review_max_read_bytes=value.source_review_max_read_bytes,
            source_review_reasoning_effort=value.source_review_reasoning_effort,
            source_review_model=value.source_review_model,
            source_review_timeout_seconds=float(value.source_review_timeout_seconds),
            l2_max_input_tokens=value.max_input_tokens,
            l2_max_output_tokens=value.max_output_tokens,
            l2_max_completion_tokens=value.max_completion_tokens,
            l2_max_cost_usd=value.max_cost_usd,
            l2_critic_reasoning_effort=value.critic_reasoning_effort,
            l2_cache_ttl_seconds=float(value.cache_ttl_seconds),
            l2_audit_retention_days=value.audit_retention_days,
        )


class CachedReviewSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    cached_at: Annotated[int, Field(ge=0)]
    effective: EffectiveReviewSettings


class ReviewSettingsCache:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> CachedReviewSettings | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            return CachedReviewSettings.model_validate_json(raw)
        except (OSError, ValueError):
            return None

    def store(self, effective: EffectiveReviewSettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = CachedReviewSettings(
            cached_at=int(time.time()), effective=effective
        ).model_dump_json()
        fd, temporary = tempfile.mkstemp(
            prefix=".review-settings-", dir=self._path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def bootstrap_review_settings(config: ScreenerConfig) -> EffectiveReviewSettings:
    # Config is env-derived and typed broadly; strict Pydantic validation is the
    # single boundary that narrows it to the platform settings contract.
    settings = ReviewSettings.model_validate(
        {
            "mode": config.l2_review_mode,
            "l2_model": config.l2_review_model,
            "l2_fallback_models": config.l2_fallback_models,
            "l3_enabled": config.l3_review_enabled,
            "l3_model": config.l3_review_model,
            "timeout_seconds": int(config.l2_timeout_seconds),
            "max_steps": config.l2_max_steps,
            "source_review_max_steps": config.source_review_max_steps,
            "source_review_max_read_bytes": config.source_review_max_read_bytes,
            "source_review_reasoning_effort": config.source_review_reasoning_effort,
            "source_review_model": config.source_review_model,
            "source_review_timeout_seconds": int(config.source_review_timeout_seconds),
            "max_input_tokens": config.l2_max_input_tokens,
            "max_output_tokens": config.l2_max_output_tokens,
            "max_completion_tokens": config.l2_max_completion_tokens,
            "max_cost_usd": config.l2_max_cost_usd,
            "critic_reasoning_effort": config.l2_critic_reasoning_effort,
            "cache_ttl_seconds": int(config.l2_cache_ttl_seconds),
            "audit_retention_days": config.l2_audit_retention_days,
            "policy_manifest_profile": (
                "l1_l2" if config.l2_review_mode == "enforce" else "l1"
            ),
            "policy_manifest_rotation_id": "policy-v10-bootstrap",
        }
    )
    payload = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return EffectiveReviewSettings(
        revision=0,
        scope="bootstrap",
        settings=settings,
        checksum=hashlib.sha256(payload).hexdigest(),
        max_age_seconds=60,
    )
