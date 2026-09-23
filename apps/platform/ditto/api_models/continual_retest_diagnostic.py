"""Read-only explanation of one exact submission's continual-retest admission."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RetestFamilyMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    canonical_composite: Annotated[float, Field(ge=0, le=1)]
    official_composite: Annotated[float, Field(ge=0)]
    representative: bool


class AdminContinualRetestDiagnostic(BaseModel):
    """A snapshot, not a ticket or permission to force a retest."""

    model_config = ConfigDict(extra="ignore")

    generated_at: datetime
    agent_id: UUID
    agent_status: str
    active_bench_version: int
    canonical_composite: float | None
    official_composite: float | None
    owner_representative_id: UUID | None
    family: list[RetestFamilyMember]
    raw_confirmation_seeds: list[str]
    folded_confirmation_seeds: list[str]
    in_raw_wave: bool
    in_emission_set: bool
    in_retest_cohort: bool
    is_same_owner_challenger: bool
    cohort_position: int | None
    cohort_size: int
    configured_cohort_size: int
    eligibility_mode: Literal["fixed", "statistical"]
    eligibility_z: float
    configured_max_size: int
    ticket_status_counts: dict[str, int]
    active_ticket_count: int
    seed_anchor_champion_id: UUID | None
    seed_anchor_block: int | None
    seed_anchor_pinned: bool | None
    admission_reason: Literal[
        "in_cohort",
        "same_owner_challenger",
        "owner_suppressed",
        "outside_cohort",
        "not_current_finalized_ledger",
    ]
