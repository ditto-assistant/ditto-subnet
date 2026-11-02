"""Stored source observations, without conferring admission authority."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto_screening_protocol import SourceReviewFinding


class StoredSourceReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    review_id: UUID
    status: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_matches: bool
    observation_status: Literal[
        "not_recorded", "finding_not_recorded", "invalid", "recorded_unverified"
    ]
    completed_at: datetime | None
    consumed_at: datetime | None
    observation_ok: bool | None = None
    clearance_certified: bool | None = None
    finding_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    finding: SourceReviewFinding | None = None
    source_admission_accepted: Literal[False] = False
    limitations: str = (
        "Matching records and canonical digests validate stored bindings only. "
        "The clearance flag is a stored reviewer assertion, not accepted "
        "source admission, health verification or private execution evidence. "
        "Missing records do not prove review never ran. A private challenge "
        "is not a universal source-admission requirement."
    )
