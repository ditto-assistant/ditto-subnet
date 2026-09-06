"""Platform-private worker handoff; never a validator-facing API."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256

Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class HostedStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-start-v2"] = Field(alias="schema")
    evaluation_id: UUID
    attempt_id: UUID
    worker_id: UUID
    agent_id: UUID
    assignment_sha256: Digest
    artifact_sha256: Digest
    screened_image_sha256: Digest
    screened_image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    screened_image_ref: Annotated[str, Field(strict=True, max_length=128)]
    screened_image_size_bytes: Annotated[int, Field(strict=True, gt=0, le=8 << 30)]
    screening_policy_version: Annotated[int, Field(strict=True, ge=9, le=1_000_000)]
    profile_capability_id: Annotated[str, Field(strict=True, max_length=128)]
    harness_instance_id: Annotated[
        str,
        Field(strict=True, min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$"),
    ]
    deadline_unix: Annotated[int, Field(strict=True, gt=0, le=253402300799)]

    @field_validator(
        "evaluation_id", "attempt_id", "worker_id", "agent_id", mode="before"
    )
    @classmethod
    def canonical_uuid(cls, value: object) -> UUID:
        if not isinstance(value, (str, UUID)):
            raise ValueError("invalid hosted start identifier")
        parsed = UUID(str(value))
        if not parsed.int or str(parsed) != str(value):
            raise ValueError("invalid hosted start identifier")
        return parsed

    @model_validator(mode="after")
    def bound_names(self) -> HostedStartRequest:
        if (
            self.screened_image_ref != f"ditto-screen/{self.agent_id}:latest"
            or self.profile_capability_id != f"hosted-{self.attempt_id}"
        ):
            raise ValueError("invalid hosted start projection")
        return self

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="hosted start",
        )
