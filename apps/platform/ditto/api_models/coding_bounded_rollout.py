"""Private single-host rollout approval; no public API or activation defaults."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto.api_models.coding_hosted_inference import Digest
from ditto.api_models.coding_hosted_runtime import PrivatePath
from ditto.api_models.coding_inference import CanonicalUUID


class RolloutEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    host_qualification: Digest
    native_matrix_acceptance: Digest
    canary_acceptance: Digest
    custody: Digest
    recovery: Digest
    rollback: Digest


class RolloutAttempt(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    evaluation_id: CanonicalUUID
    attempt_id: CanonicalUUID
    worker_id: CanonicalUUID
    config_file: PrivatePath
    config_sha256: Digest
    assignment_sha256: Digest
    policy_sha256: Digest
    deadline_unix: Annotated[int, Field(gt=0)]
    cost_ceiling_usd_micros: Annotated[int, Field(ge=1, le=100_000_000)]


class BoundedRolloutApproval(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    schema_name: Literal["dittobench-coding-bounded-rollout-v2"] = Field(alias="schema")
    purpose: Literal["shadow-cohort-once"]
    runtime_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    runtime_archive_sha256: Digest
    controller_sha256: Digest
    machine_id_sha256: Digest
    boot_id: CanonicalUUID
    connectivity_file: PrivatePath
    connectivity_sha256: Digest
    accepted_evidence: RolloutEvidence
    issued_at_unix: Annotated[int, Field(gt=0)]
    expires_at_unix: Annotated[int, Field(gt=0)]
    max_attempts: Annotated[int, Field(ge=1, le=64)]
    max_parallel: Annotated[int, Field(ge=1, le=4)]
    max_reserved_cost_usd_micros: Annotated[int, Field(ge=1, le=6_400_000_000)]
    allow_candidate_failure: bool
    attempts: Annotated[tuple[RolloutAttempt, ...], Field(min_length=1, max_length=64)]
    shadow_only: Literal[True]
    weight_eligible: Literal[False]

    @field_validator(
        "shadow_only", "weight_eligible", "allow_candidate_failure", mode="before"
    )
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("rollout flags require booleans")
        return value

    @model_validator(mode="after")
    def bounded(self) -> BoundedRolloutApproval:
        if (
            len(self.attempts) != self.max_attempts
            or self.max_parallel > self.max_attempts
            or not 0 < self.expires_at_unix - self.issued_at_unix <= 82800
            or sum(item.cost_ceiling_usd_micros for item in self.attempts)
            > self.max_reserved_cost_usd_micros
            or any(
                not self.issued_at_unix < item.deadline_unix <= self.expires_at_unix
                for item in self.attempts
            )
        ):
            raise ValueError("rollout bounds differ")
        for field in ("evaluation_id", "attempt_id", "config_file"):
            if len({getattr(item, field) for item in self.attempts}) != len(
                self.attempts
            ):
                raise ValueError("rollout attempts are not unique")
        digests = [
            self.runtime_archive_sha256,
            self.controller_sha256,
            self.machine_id_sha256,
            self.connectivity_sha256,
            *self.accepted_evidence.model_dump().values(),
        ]
        digests.extend(
            value
            for item in self.attempts
            for value in (
                item.config_sha256,
                item.assignment_sha256,
                item.policy_sha256,
            )
        )
        if self.runtime_revision == "0" * 40 or any(
            value == "0" * 64 for value in digests
        ):
            raise ValueError("rollout approval commitments are absent")
        return self
