"""Read-only paid artifact comparisons; no admission or integrity authority."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

AttemptKind = Literal[
    "first_submission",
    "infrastructure_retry",
    "packaging_only_repair",
    "small_source_delta",
    "material_new_work",
    "inconclusive",
]


class AttemptObservationPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    report_only: Literal[True] = True
    admission_effect: Literal["none"] = "none"
    source_clearance: Literal[False] = False
    integrity_clearance: Literal[False] = False
    classifier_version: int
    source_build: str
    settings_digest: str
    reference_corpus: dict[str, str]
    max_archive_bytes: int
    max_unpacked_bytes: int
    max_members: int
    max_owner_links: int
    small_delta_jaccard: float


class AttemptComparison(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    policy: AttemptObservationPolicy
    agent_id: UUID
    reference_agent_id: UUID | None = None
    as_of: datetime
    classification: AttemptKind
    reason: str
    sha256: str
    reference_sha256: str | None = None
    feedback_status: Literal["infrastructure", "repairable", "completed", "pending"] = (
        "pending"
    )
    feedback_reason: str | None = None
    feedback_at: datetime | None = None
