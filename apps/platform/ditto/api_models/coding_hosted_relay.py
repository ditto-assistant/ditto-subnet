"""Private local-worker relay messages; never part of public OpenAPI."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_inference import Digest
from ditto.api_models.coding_inference import BoundedIdentity, CanonicalUUID


class HostedRelayBinding(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)
    schema_name: Literal["dittobench-coding-hosted-relay-binding-v2"] = Field(
        alias="schema"
    )
    evaluation_id: CanonicalUUID
    attempt_id: CanonicalUUID
    worker_id: CanonicalUUID
    grant_id: CanonicalUUID
    assignment_sha256: Digest
    policy_sha256: Digest
    artifact_sha256: Digest
    harness_instance_id: BoundedIdentity
    profile_capability_id: BoundedIdentity
    expires_at_unix: Annotated[int, Field(strict=True, gt=0)]

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=4096,
            label="relay binding",
        )


class HostedRelayCommand(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)
    schema_name: Literal["dittobench-coding-hosted-relay-command-v2"] = Field(
        alias="schema"
    )
    binding_sha256: Digest
    operation: Literal["open", "complete", "revoke"]
    request_id: CanonicalUUID
    miner_request_base64: Annotated[str, Field(strict=True, max_length=6 << 20)] = ""
