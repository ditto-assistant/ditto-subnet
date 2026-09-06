"""Private local runtime configuration, never included in public OpenAPI."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ditto.api_models.coding_hosted_start import Digest

PrivatePath = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class HostedRuntimeHostSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    docker_executable: PrivatePath
    docker_socket: PrivatePath
    router_listen: Name
    egress_network: Name
    egress_proxy: Name
    executor_repository: Name
    candidate_uid: Annotated[int, Field(strict=True, ge=1, le=4294967295)]
    candidate_gid: Annotated[int, Field(strict=True, ge=1, le=4294967295)]
    seccomp_profile: Annotated[str, Field(strict=True, max_length=4096)] = ""
    apparmor_profile: Annotated[str, Field(strict=True, max_length=4096)] = ""


class HostedPlatformRuntimeInput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    schema_name: Literal["dittobench-coding-hosted-platform-runtime-v2"] = Field(
        alias="schema"
    )
    shadow_only: Literal[True]
    weight_eligible: Literal[False]
    worker_id: UUID
    evaluation_id: UUID
    attempt_id: UUID
    assignment_sha256: Digest
    runtime_root: PrivatePath
    worker_executable: PrivatePath
    python_executable: PrivatePath
    unwrap_executable: PrivatePath
    unwrap_work_root: PrivatePath
    postgres_environment_file: PrivatePath
    hippius_environment_file: PrivatePath
    image_storage_file: PrivatePath
    provider_key_file: PrivatePath
    execution_profile_file: PrivatePath
    grading_profile_file: PrivatePath
    budget_profile_file: PrivatePath
    policy_file: PrivatePath
    transport_manifest_file: PrivatePath
    payload_authority_file: PrivatePath
    publication_receipt_file: PrivatePath
    curator_public_key_file: PrivatePath
    evidence_public_key_file: PrivatePath
    evidence_wrapping_key_sha256: Digest
    probe_receipt_file: PrivatePath
    host: HostedRuntimeHostSettings
    spool_max_bytes: Annotated[int, Field(strict=True, ge=256 << 20, le=16 << 30)] = (
        2 << 30
    )
    spool_max_objects: Annotated[int, Field(strict=True, ge=64, le=100_000)] = 4096

    @field_validator("worker_id", "evaluation_id", "attempt_id", mode="before")
    @classmethod
    def canonical_uuid(cls, value: object) -> UUID:
        if not isinstance(value, (UUID, str)):
            raise ValueError("runtime identifier is invalid")
        parsed = UUID(str(value))
        if not parsed.int or str(parsed) != str(value):
            raise ValueError("runtime identifier is invalid")
        return parsed

    @field_validator("shadow_only", "weight_eligible", mode="before")
    @classmethod
    def boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("runtime flag is invalid")
        return value

    def __repr__(self) -> str:
        return "HostedPlatformRuntimeInput(private=True)"

    def __str__(self) -> str:
        return self.__repr__()


class HostedRuntimeImageStorage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    endpoint_url: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    bucket: Name
    access_key: Name
    secret_key: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    region: Name

    def __repr__(self) -> str:
        return "HostedRuntimeImageStorage(private=True)"

    def __str__(self) -> str:
        return self.__repr__()
