"""Private Platform process messages; never mounted in public OpenAPI."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_inference import Digest
from ditto.api_models.coding_inference import BoundedIdentity, CanonicalUUID


class SourceBinding(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    evaluation_id: CanonicalUUID
    attempt_id: CanonicalUUID
    worker_id: CanonicalUUID
    assignment_sha256: Digest
    artifact_sha256: Digest
    harness_instance_id: BoundedIdentity
    profile_capability_id: BoundedIdentity
    deadline_unix: Annotated[int, Field(gt=0)]

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json"), maximum_bytes=4096, label="hosted source"
        )


class RetentionHeader(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    completed_run: bool
    freeze_sha256: Digest
    freeze_size: Annotated[int, Field(ge=1, le=520 << 20)]
    transcript_sha256: Digest
    transcript_size: Annotated[int, Field(ge=0, le=512 << 20)]


class ControlCommand(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )
    schema_name: Literal["dittobench-coding-hosted-control-command-v2"] = Field(
        alias="schema"
    )
    request_id: CanonicalUUID
    operation: Literal[
        "authoring",
        "check",
        "inference",
        "revoke",
        "close",
        "retain",
        "freeze",
        "abort",
        "grading",
        "check_grading",
        "grading_bundle",
        "terminal",
    ]
    source: SourceBinding
    retention: RetentionHeader | None = None
    evidence_sha256: Digest | None = None
    patch_size: Annotated[int, Field(ge=0, le=128 << 20)] = 0
    terminal_size: Annotated[int, Field(ge=0, le=4 << 20)] = 0
    terminal_sha256: Digest | None = None
    claim_id: CanonicalUUID | None = None


class AuthoringBlob(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    schema_name: Literal["dittobench-coding-authoring-blob-v2"] = Field(alias="schema")
    object_id: CanonicalUUID
    source_sha256: Digest
    payload_sha256: Digest
    ordinal: Annotated[int, Field(ge=-1, le=130)]
    plaintext_sha256: Digest
    plaintext_size: Annotated[int, Field(ge=1, le=24 << 20)]
    ciphertext_sha256: Digest
    ciphertext_size: Annotated[int, Field(ge=17, le=(24 << 20) + 2048)]
    wrapping_key_sha256: Digest
    envelope_sha256: Digest
    storage_domain_sha256: Digest


class AuthoringIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    schema_name: Literal["dittobench-coding-authoring-evidence-v2"] = Field(
        alias="schema"
    )
    source: SourceBinding
    header: RetentionHeader
    patch_sha256: Digest | None
    patch_size: Annotated[int, Field(ge=0, le=128 << 20)]
    inference_evidence_sha256: Digest | None
    chunk_count: Annotated[int, Field(ge=1, le=130)]
    chunks_sha256: Digest
    manifest: AuthoringBlob
    publication_deadline_unix: Annotated[int, Field(gt=0)]
    weight_eligible: Literal[False]

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="authoring evidence",
        )
