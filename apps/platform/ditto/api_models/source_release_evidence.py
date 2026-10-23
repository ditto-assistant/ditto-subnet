"""Bounded publication evidence for exact-artifact provenance review."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.source_disclosure import SourceDisclosure


class SourceReleaseEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    artifact_sha256: str
    candidate_agent_id: UUID | None
    reference_time: datetime
    policy_revision: int
    disclosure: SourceDisclosure
    embargo_hours: int = Field(ge=6, le=8760)
    emission_confirmed_at: datetime | None
    derived_eligibility_at: datetime | None
    publication_evidence_at: datetime | None
    publication_proven_by_reference_time: bool
    exact_sha_public_handoff_count: int = Field(ge=0)
    earliest_exact_sha_public_handoff_at: datetime | None
    absence_proves_nonpublication: Literal[False] = False
    limitations: str = (
        "Public fetch auditing is fail-open. Missing rows do not prove no public "
        "handoff. Eligibility uses the policy at the reference time and current "
        "source status; an earlier shorter release window may be omitted. "
        "Derived eligibility is not historical availability proof: emission "
        "confirmation may arrive late with an earlier payout-block time. "
        "Private operator, miner, screener and validator reads are not publication."
    )
