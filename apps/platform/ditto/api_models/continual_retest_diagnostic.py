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
    route_priority: Literal[
        "champion", "void_rerun", "catchup", "emission", "extended", "not_routed"
    ]
    """``void_rerun``: owes a replacement for an infrastructure-voided sample,
    which the lane issues right behind the crown regardless of cohort."""
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
    void_rerun_seed_count: int = 0
    """Voided seeds still owed a replacement run (a count; seeds stay private).

    Each void owes exactly one replacement for its seed; the count drops as
    replacements land and never exceeds the number of voids."""
    void_rerun_claimable: bool = False
    """Whether a validator polling now could lease one of those seeds."""
    provider_outage_active: bool = False
    """Provider circuit open or inside its recovery quiet window; void re-run
    debt is withheld until the provider is healthy again."""
    provider_gate_closed: bool = False
    """The provider-outage gate admits no new lease right now (open circuit
    cooling down, or its one half-open probe already claimed). The lane then
    issues nothing on any route and the decision is ``provider_outage``."""
    retest_mode: Literal["continual", "season_duel"] = "continual"
    """The lane's retest mode (``docs/title-duels.md``)."""
    season_duel_seed_count: int | None = None
    """Shared seeds in the current season's duel, or null when season duels do
    not govern this version (``continual`` mode, or below the dueling floor)."""
    season_duel_anchor_champion_id: UUID | None = None
    """The champion the season's duel family is anchored on: the one that
    opened the season, which a mid-season crown change does not move."""
    season_duel_held_seed_count: int | None = None
    """Duel seeds this agent already ran this season (a count; seeds stay
    private). Equal to ``season_duel_seed_count`` once its duel is complete."""
    wave_cadence_tempos: int = 0
    """Tempo-aligned wave pacing in force (0: historical pacing)."""
    wave_seeds_per_window: int | None = None
    wave_window_epoch: int | None = None
    """First chain epoch of the current pacing window, or null when unpaced
    (cadence 0, or no ledger pin to anchor a window -- pacing fails open)."""
    wave_next_window_epoch: int | None = None
    wave_window_start: datetime | None = None
    wave_window_opened_seed_count: int | None = None
    """Fresh shared seeds already opened in this window (a count; seeds stay
    private)."""
    wave_growth_paused: bool = False
    """The window's seed budget is spent: no fresh shared seed opens until
    ``wave_next_window_epoch``. Catch-up and open waves still lease."""
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
        "provider_outage",
    ]


class RetestFailureCode(BaseModel):
    model_config = ConfigDict(extra="ignore")

    terminal_error_code: str
    count: int


class RetestSampleInference(BaseModel):
    """One accepted confirmation sample and the inference grant that served it.

    Counts only. A sample whose grant shows many failed chat calls measured the
    inference path as much as the agent; read ``failure_codes`` before treating
    a low sample as the agent's own behaviour.
    """

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: str
    seed: str
    """Exact decimal seed; int63 does not survive a JavaScript number."""
    composite: Annotated[float, Field(ge=0, le=1)]
    run_id: str
    accepted_at: datetime
    grant_id: UUID | None = None
    """Newest grant for this validator created at or before acceptance."""
    chat_requests: int = 0
    failed_requests: int = 0
    failure_codes: list[RetestFailureCode] = []
    """Failed chat calls by relay ``terminal_error_code``, most frequent first."""
    void_generation: int = 0
    """0 for a first result; ``n`` for the re-run after ``n`` infra voids."""
    voided: bool = False
    """Set aside as infrastructure; listed for audit, counts toward nothing."""


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
    confirmation_seed_floor: int = 0
    """Shared seeds the lane gathers before variance may stop it (minimum)."""
    confirmation_seed_cap: int = 0
    """The continual-retest work cap: the most shared seeds the lane ever
    targets and the size of the active window the official composite folds."""
    confirmation_seed_target: int | None = None
    """The variance-sized seed target the lane is planning toward right now,
    between floor and cap, from the raw emission set's recorded composites;
    ``None`` when there is no current emission set."""
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
    retest_samples: list[RetestSampleInference] = []
    """Newest confirmation samples (at most 30) with serving-grant failures."""
