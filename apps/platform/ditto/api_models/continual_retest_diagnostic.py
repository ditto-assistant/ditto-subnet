"""Read-only explanation of one exact submission's continual-retest admission."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.continual_retest_settings import WaveMembership


class RetestFamilyMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    canonical_composite: Annotated[float, Field(ge=0, le=1)]
    official_composite: Annotated[float, Field(ge=0)]
    representative: bool
    effective_composite: Annotated[float | None, Field(ge=0)] = None
    """The curve-v3 secondary that breaks an exact official-quality tie."""
    canonical_sample_count: int = 0
    """Signed quorum composites behind ``canonical_composite`` (3 = 3/3)."""
    completed_wave_depth: int = 0
    """Fold-eligible shared seeds behind ``official_composite``."""
    first_seen: datetime | None = None
    """The fold's lineage clock for this generation, not its upload time."""


class RetestCutoffComparison(BaseModel):
    """One cutoff this agent was measured against, with the arithmetic."""

    model_config = ConfigDict(extra="ignore")

    agent_id: UUID | None = None
    """The agent holding the cutoff, or null when no cutoff was reached."""
    composite: float | None = None
    """The cutoff's official composite, on the same scale as the candidate's."""
    gap: float | None = None
    """``cutoff - candidate``; negative when the candidate is already ahead."""
    tie_band: float | None = None
    """``z * sqrt(se_candidate^2 + se_cutoff^2)``; zero under a fixed rank."""
    within_tie_band: bool | None = None
    """Whether the gap is inside the band the cohort builder applied."""


class RetestClaimability(BaseModel):
    """Whether a validator polling now could lease this exact agent.

    A projection of the issuance lane's own predicates, in the order the lane
    evaluates them. Reading it never issues, reserves, or reprioritizes work.
    """

    model_config = ConfigDict(extra="ignore")

    lane_enabled: bool
    latest_block: int | None
    """Null when the chain read failed; every gate below it stays unresolved."""
    champion_agent_id: UUID | None
    champion_crown_block: int | None
    scheduled_round: bool | None
    """Whether the reign-backoff round is due at ``latest_block``."""
    spare_capacity_window: bool
    """Whether canonical quorum work is draining somewhere in the fleet."""
    idle_retests_enabled: bool
    in_catchup_set: bool
    """Whether this agent owes backlog seeds no live lease is covering."""
    route_priority: Literal["champion", "catchup", "emission", "extended", "not_routed"]
    route_position: int | None
    """1-based place in the auto-route candidate order, or null when absent."""
    pending_seed_count: int
    """Seeds this agent still owes. Seed values are deliberately not returned."""
    claimable_seed_available: bool
    live_lease_count: int
    newer_canonical_work_pending: bool
    """A newer same-miner submission is still awaiting canonical quorum."""
    least_covered_admitted: bool | None
    """The fairness gate's answer, or null when no seed was claimable anyway."""
    decision: Literal[
        "claimable",
        "lane_disabled",
        "not_in_cohort",
        "chain_unavailable",
        "round_not_due",
        "newer_canonical_work_pending",
        "no_pending_seeds",
        "all_pending_seeds_leased",
        "another_member_less_covered",
    ]


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
    ledger_eligible: bool = False
    """Whether this generation is in the current finalized eligible ledger."""
    canonical_sample_count: int = 0
    completed_wave_depth: int = 0
    official_sample_count: int = 0
    """Observations behind the official continual composite (quorum + waves)."""
    raw_confirmation_depth: int = 0
    """Distinct accepted shared seeds, including any not yet fold-eligible."""
    composite_stderr: float | None = None
    aggregate_mode: Literal["disabled", "fleet_ready", "enabled"] = "fleet_ready"
    wave_membership: WaveMembership = "participants"
    owner_key: str | None = None
    """The emission-owner root this generation folds under."""
    representative_canonical_composite: float | None = None
    representative_official_composite: float | None = None
    representative_margin: float | None = None
    """``representative official - this official``; positive means behind."""
    representative_selection: Literal[
        "self",
        "official_composite",
        "efficiency_tiebreak",
        "newest_generation",
        "agent_id_tiebreak",
        "none",
    ] = "none"
    cohort_cutoff: RetestCutoffComparison = RetestCutoffComparison()
    emission_cutoff: RetestCutoffComparison = RetestCutoffComparison()
    claim: RetestClaimability | None = None
    latest_ticket_status: str | None = None
    latest_ticket_validator_hotkey: str | None = None
    latest_ticket_updated_at: datetime | None = None
    latest_ticket_failure_reason: str | None = None
    terminal_ticket_count: int = 0
    latest_confirmation_composite: float | None = None
    latest_confirmation_recorded_at: datetime | None = None
