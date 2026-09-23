"""Report-only, exact-artifact source-verification replay wire shapes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PublicCheck = Literal[
    "archive_sha",
    "build_image_digest",
    "health",
    "ordinary_model_run",
    "tool_selection_run",
    "seed_memory_run",
    "two_user_isolation",
]


class VerificationReplayCreate(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    request_id: UUID
    quarantine_id: UUID
    source_attempt_id: UUID
    artifact_sha256: Sha256
    policy_version: Literal[13]
    image_upload_id: UUID | None = None
    image_sha256: Sha256 | None = None
    expected_agent_status: Literal["quarantined"]
    actor: Annotated[str, Field(min_length=1, max_length=120)]
    reason: Annotated[str, Field(min_length=8)]

    @model_validator(mode="after")
    def image_pair(self) -> VerificationReplayCreate:
        if (self.image_upload_id is None) != (self.image_sha256 is None):
            raise ValueError(
                "verified image identity and SHA must be supplied together"
            )
        return self


class VerificationReplayState(BaseModel):
    replay_id: UUID
    request_id: UUID
    agent_id: UUID
    quarantine_id: UUID
    source_attempt_id: UUID
    artifact_sha256: str
    policy_version: int
    image_upload_id: UUID | None
    image_sha256: str | None
    image_size_bytes: int | None
    image_id: str | None = Field(
        ...,
        description=(
            "Worker-claimed Docker image ID; not verified against the tar contents"
        ),
    )
    image_staging_id: UUID | None
    image_verified_at: datetime | None
    image_verified_storage_key: str | None
    status: Literal["queued", "running", "reported", "failed"]
    policy_verification_complete: Literal[False] = False
    worker_hotkey: str | None
    lease_deadline: datetime | None
    created_at: datetime
    finished_at: datetime | None
    failure_code: str | None
    receipt_count: int = 0


class VerificationReplayInputs(BaseModel):
    replay: VerificationReplayState
    artifact_url: str
    image_url: str | None
    urls_expire_at: datetime


class VerificationReplayClaimability(BaseModel):
    replay_id: UUID
    original_screener_hotkey: str
    source_binding_current: bool
    replay_enabled_independent_hotkeys: list[str]
    independent_replay_enabled: bool
    note: str


class VerificationReplayReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    artifact_sha256: Sha256
    policy_version: Literal[13]
    image_upload_id: UUID | None = None
    image_sha256: Sha256 | None = None
    check_code: PublicCheck
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def image_binding(self) -> VerificationReplayReceiptRequest:
        if self.check_code != "archive_sha" and self.image_sha256 is None:
            raise ValueError("runtime replay receipt requires verified image SHA")
        return self


class VerificationReplayReceiptState(BaseModel):
    receipt_id: UUID
    replay_id: UUID
    check_code: PublicCheck
    evidence_sha256: str
    worker_hotkey: str
    created_at: datetime


class VerificationReplayFinish(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    status: Literal["reported", "failed"]
    failure_code: Annotated[str | None, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] = (
        None
    )
    artifact_sha256: Sha256
    image_sha256: Sha256 | None = None


class VerificationReplayBuildUploadRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    artifact_sha256: Sha256
    image_sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=8 * 1024 * 1024 * 1024)]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class VerificationReplayBuildUpload(BaseModel):
    replay_id: UUID
    upload_url: str
    required_headers: dict[str, str]
    expires_at: datetime


class VerificationReplayBuildVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    artifact_sha256: Sha256
    image_sha256: Sha256
    size_bytes: Annotated[int, Field(gt=0, le=8 * 1024 * 1024 * 1024)]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
