"""Shadow-only core tool-and-memory qualification contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ScoreValue = Annotated[float, Field(ge=0.0, le=1.0)]


class CoreQualificationModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        strict=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


class CoreQualificationPolicy(CoreQualificationModel):
    """One benchmark-scoped shadow qualification policy."""

    schema_name: Literal["ditto-core-qualification-policy-v1"] = Field(alias="schema")
    weight_eligible: Literal[False] = False
    bench_version: Annotated[int, Field(ge=7)]
    enter_composite: ScoreValue
    enter_tool_mean: ScoreValue
    enter_memory_mean: ScoreValue
    exit_composite: ScoreValue
    exit_tool_mean: ScoreValue
    exit_memory_mean: ScoreValue
    enter_observations: Annotated[int, Field(ge=1, le=20)]
    exit_observations: Annotated[int, Field(ge=1, le=20)]

    @model_validator(mode="after")
    def exit_floors_do_not_exceed_entry_floors(self) -> CoreQualificationPolicy:
        for dimension in ("composite", "tool_mean", "memory_mean"):
            if getattr(self, f"exit_{dimension}") > getattr(self, f"enter_{dimension}"):
                raise ValueError(f"exit_{dimension} cannot exceed enter_{dimension}")
        return self


def core_qualification_policy_checksum(policy: CoreQualificationPolicy) -> str:
    body = (
        json.dumps(
            policy.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return hashlib.sha256(body).hexdigest()


class CoreQualificationPolicyRevision(CoreQualificationModel):
    revision: Annotated[int, Field(ge=1)]
    parent_revision: Annotated[int, Field(ge=0)]
    policy: CoreQualificationPolicy
    checksum: Sha256
    reason: str
    actor: str
    created_at: datetime


class AdminCoreQualificationPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    expected_revision: Annotated[int, Field(ge=0)]
    policy: CoreQualificationPolicy
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least 8 non-whitespace characters")
        return value.strip()

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class AdminCoreQualificationPolicyResponse(CoreQualificationModel):
    bench_version: Annotated[int, Field(ge=7)]
    configured: bool
    current: CoreQualificationPolicyRevision | None
    history: list[CoreQualificationPolicyRevision]
    required_confirmation: str
    shadow_only: Literal[True] = True


class AdminCoreQualificationRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    bench_version: Annotated[int, Field(ge=7)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least 8 non-whitespace characters")
        return value.strip()

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


CoreQualificationDecision = Literal[
    "partial_wave",
    "below_entry",
    "pending_entry",
    "entered",
    "held",
    "pending_exit",
    "exited",
]


class CoreQualificationObservation(CoreQualificationModel):
    sequence: Annotated[int, Field(ge=1)]
    observation_id: UUID
    agent_id: UUID
    artifact_sha256: Sha256
    screened_image_sha256: Sha256
    bench_version: Annotated[int, Field(ge=7)]
    policy_revision: Annotated[int, Field(ge=1)]
    policy_checksum: Sha256
    score_evidence_sha256: Sha256
    score_count: Annotated[int, Field(ge=3)]
    full_size: bool
    complete_wave: bool
    validator_hotkeys: Annotated[list[str], Field(min_length=3)]
    run_ids: Annotated[list[str], Field(min_length=3)]
    median_composite: ScoreValue
    median_tool_mean: ScoreValue
    median_memory_mean: ScoreValue
    entry_passed: bool
    retention_passed: bool
    qualified: bool
    enter_streak: Annotated[int, Field(ge=0, le=20)]
    exit_streak: Annotated[int, Field(ge=0, le=20)]
    decision: CoreQualificationDecision
    source: Literal["score_commit", "admin_refresh"]
    actor: str | None
    reason: str | None
    observed_at: datetime
    weight_eligible: Literal[False] = False
    current: bool = False
    stale_reason: Literal[
        "current",
        "artifact_changed",
        "screened_image_changed",
        "benchmark_changed",
        "policy_changed",
    ]

    @model_validator(mode="after")
    def observation_is_coherent(self) -> CoreQualificationObservation:
        if len(self.validator_hotkeys) != self.score_count or len(self.run_ids) != (
            self.score_count
        ):
            raise ValueError("score evidence identities do not match score_count")
        if self.complete_wave == (self.decision == "partial_wave"):
            raise ValueError("partial-wave decision disagrees with complete_wave")
        if self.source == "score_commit" and (
            self.actor is not None or self.reason is not None
        ):
            raise ValueError("automatic observation cannot carry operator audit fields")
        if self.source == "admin_refresh" and (
            self.actor is None
            or not self.actor.strip()
            or len(self.actor.strip()) > 120
            or self.reason is None
            or len(self.reason.strip()) < 8
        ):
            raise ValueError("admin refresh requires a bounded actor and reason")
        if self.decision != "partial_wave":
            qualified_decision = self.decision in {
                "entered",
                "held",
                "pending_exit",
            }
            if self.qualified != qualified_decision:
                raise ValueError("qualification decision disagrees with state")
        return self


class AgentCoreQualificationStatus(CoreQualificationModel):
    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    artifact_sha256: Sha256
    screened_image_sha256: Sha256 | None
    bench_version: Annotated[int, Field(ge=7)]
    configured: bool
    qualified: bool
    current_observation: CoreQualificationObservation | None
    total: Annotated[int, Field(ge=0)]
    observations: list[CoreQualificationObservation]
    shadow_only: Literal[True] = True
