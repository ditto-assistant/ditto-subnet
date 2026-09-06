"""Private native terminal evidence identity."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_hosted_control import AuthoringBlob, SourceBinding
from ditto.api_models.coding_hosted_inference import Digest
from ditto.api_models.coding_inference import CanonicalUUID


class HostedTerminalIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    schema_name: Literal["dittobench-coding-terminal-evidence-v2"] = Field(
        alias="schema"
    )
    source: SourceBinding
    claim_id: CanonicalUUID
    authoring_evidence_sha256: Digest
    grading_profile_sha256: Digest
    result_sha256: Digest
    outcome: Literal[
        "completed", "candidate_failure", "infrastructure_failure", "integrity_failure"
    ]
    blob: AuthoringBlob
    publication_deadline_unix: Annotated[int, Field(gt=0)]
    weight_eligible: Literal[False]

    def digest(self) -> str:
        return coding_canonical_sha256(
            self.model_dump(mode="json", by_alias=True),
            maximum_bytes=16384,
            label="terminal identity",
        )
