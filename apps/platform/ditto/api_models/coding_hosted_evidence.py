"""Native inference-evidence commitments, never v1 ticket identities."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_inference import Digest
from ditto.api_models.coding_inference import CanonicalUUID

MAX_PLAINTEXT = 24 << 20


class HostedEvidenceIdentity(BaseModel):
    model_config = ConfigDict(
        extra="ignore", frozen=True, strict=True, populate_by_name=True
    )
    schema_name: Literal["dittobench-coding-hosted-inference-evidence-v2"] = Field(
        alias="schema"
    )
    reservation_id: CanonicalUUID
    request_id: CanonicalUUID
    grant_id: CanonicalUUID
    evaluation_id: CanonicalUUID
    attempt_id: CanonicalUUID
    worker_id: CanonicalUUID
    assignment_sha256: Digest
    policy_sha256: Digest
    settlement_sha256: Digest
    runtime_profile_sha256: Digest
    storage_domain_sha256: Digest
    wrapping_key_sha256: Digest
    plaintext_sha256: Digest
    plaintext_size: Annotated[int, Field(ge=1, le=MAX_PLAINTEXT)]
    ciphertext_sha256: Digest
    ciphertext_size: Annotated[int, Field(ge=17, le=MAX_PLAINTEXT + 2048)]
    envelope_sha256: Digest
    publication_deadline_unix: Annotated[int, Field(gt=0)]
    weight_eligible: Literal[False]

    @model_validator(mode="after")
    def sizes(self) -> "HostedEvidenceIdentity":
        if not 16 < self.ciphertext_size - self.plaintext_size <= 2048:
            raise ValueError("hosted ciphertext size does not match AEAD framing")
        return self

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="hosted evidence identity",
        )
