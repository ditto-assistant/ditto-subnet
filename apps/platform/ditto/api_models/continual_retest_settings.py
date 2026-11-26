"""Hot-swappable operator policy for continual top-five retests."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The KOTH emission set is five (champion + four-entry participation tail) and is
# frozen consensus shared with ``ditto-subnet``'s weight fold. It is the floor of
# the retest cohort, never a value the operator can lower: the shared-seed wave
# exists to keep exactly those five comparable, so a cohort that excluded one of
# them would stop the lane doing its only consensus-relevant job.
EMISSION_SET_SIZE = 5

WaveMembership = Literal["strict", "participants", "per_agent"]
RetestMode = Literal["continual", "season_duel"]

DEFAULT_WAVE_MEMBERSHIP: WaveMembership = "participants"
"""The shipped fold policy -- single source of truth for every default.

Deliberately NOT ``strict``. Kept here, and re-exported through
``ditto.db.queries.confirmation_scores``, so that the operator policy model and
every helper taking a ``wave_membership`` argument cannot drift apart. That
drift would be a live hazard rather than a tidiness concern: the champion those
helpers derive is the seed anchor, and two answers on two paths derive two
different seed families.

See :attr:`ContinualRetestSettings.wave_membership` for why ``participants``.
"""
# Ceiling on how far past the emission set an operator may extend retesting.
# Every extra member is real validator work on every wave seed, so the cap keeps
# an accidental "retest everyone" from consuming the fleet.
MAX_RETEST_COHORT_SIZE = 25
# Ceiling on the tie-tolerance band, in standard errors. The fold's own dethrone
# test uses ``KOTH_DETHRONE_Z = 1.64`` (one-sided 95%), which is the natural
# default: an agent that could statistically dethrone the cutoff is exactly an
# agent whose ranking against the cutoff is unsettled. Three is a hard stop --
# beyond it the band stops meaning "tied" and starts meaning "nearby", and the
# only thing still bounding the cohort would be the size ceiling.
MAX_RETEST_ELIGIBILITY_Z = 3.0
# Tempo-aligned wave pacing (``wave_cadence_tempos``). Twenty-four epochs is
# about a day of SN118 tempos; beyond it a "wave" stops tracking the chain.
MAX_WAVE_CADENCE_TEMPOS = 24
MAX_WAVE_SEEDS_PER_WINDOW = 16
DEFAULT_WAVE_SEEDS_PER_WINDOW = 3
DEFAULT_RETEST_ELIGIBILITY_Z = 1.64
# Season duels (``retest_mode = season_duel``, ``docs/title-duels.md``): each
# dueling season draws this many shared seeds. Two is the paired statistic's
# own floor; five keeps a season's duel at most 25 runs for the top five.
MIN_SEASON_DUEL_SEEDS = 2
MAX_SEASON_DUEL_SEEDS = 5
DEFAULT_SEASON_DUEL_SEEDS = 3


# The first validator heartbeat protocol whose fold reads ``crown_mode`` and
# ``crown_incumbent_agent_id`` off the ledger. Exposing the marker to an older
# fleet would fold two different champions, so it is withheld until every
# recently-live weight setter reports at least this.
CROWN_INCUMBENT_PROTOCOL = 27

# The first validator heartbeat protocol whose fold reads
# ``provisional_incumbent``: a crown incumbent withheld by the terminal-review
# emission gate keeps the crown and its shares burn. An enforcing gate filters
# the pool only once every recently-live weight setter reports at least this;
# an older validator would crown and pay the runner-up instead.
PROVISIONAL_INCUMBENT_PROTOCOL = 28

# The first validator heartbeat protocol whose fold reads ``crown_gate``: the
# minimum shared-seed confirmations a challenger needs before the paired
# comparison may move the crown, and the official standing the crown must keep.
CROWN_GATE_PROTOCOL = 34
# The first validator heartbeat protocol whose fold reads
# ``crown_gate.duel_window_pairs`` and ``LedgerEntry.duel_history``: the
# sequential cross-season title duel (``docs/title-duels.md``, phase B).
CROWN_DUEL_PROTOCOL = 35
# Dueling seasons whose confirmation records the ledger serves as title-duel
# evidence: five 3-seed seasons fill the 15-pair window.
CROWN_DUEL_SEASONS = 5
DEFAULT_MIN_DETHRONE_CONFIRMATIONS = 3
DEFAULT_CHAMPION_STANDING_RANK = 3
# A challenger cannot hold more shared seeds than the continual lane retains:
# mirror ``ditto.api_server.koth.TOP5_MAX_CONFIRMATION_SEEDS`` (the work cap and
# active fold window). New writes are bounded by it and the served crown gate is
# clamped to it. Every stored revision so far uses 3.
MAX_MIN_DETHRONE_CONFIRMATIONS = 10
# Revisions written under the former 15-seed cap still parse faithfully (their
# stored checksum covers the stored value), up to the validator's fold ceiling.
MAX_STORED_MIN_DETHRONE_CONFIRMATIONS = 15


class LedgerPinStatus(BaseModel):
    """Identity of the epoch-pinned ledger currently served to validators."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    epoch_index: int
    last_epoch_block: int
    pinned_block: int
    pinned_at: datetime
    bench_version: int
    ledger_digest: str
    entry_count: int
    champion_agent_id: UUID | None = None
    incumbent_agent_id: UUID | None = None


class ContinualRetestSettings(BaseModel):
    """Complete subnet-global continual-retest policy stored per revision."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    aggregate_mode: Literal["disabled", "fleet_ready", "enabled"] = "fleet_ready"

    tie_weighting_mode: Literal["disabled", "fleet_ready"] = "disabled"
    """Whether statistically tied emission slots pool their rank shares.

    ``disabled`` is the historical fixed ``65/14/10/7/4`` fold and the exact
    rollback. ``fleet_ready`` activates only after every recently-live
    weight-setting validator reports the protocol that understands the ledger
    marker. There is deliberately no unconditional override: unlike background
    retest planning, a mixed weight fold is a consensus split.
    """

    statistical_band_mode: Literal["disabled", "fleet_ready"] = "disabled"
    """Activate the protocol-29 statistical cap only after operator selection
    and every recently-live weight setter reports support. The default keeps
    the historical fold throughout a rolling upgrade."""

    crown_gate_mode: Literal["disabled", "fleet_ready"] = "disabled"
    """Whether the KOTH fold gates the crown on shared-seed evidence (protocol 34).

    ``disabled`` is the pre-34 walk and the exact rollback: any challenger that
    clears the indifference band takes the crown, including on an unpaired
    comparison of a newcomer's three-seed canonical against the incumbent's
    retest-folded score. ``fleet_ready`` serves ``crown_gate`` once every
    recently-live weight setter reports protocol 34: the crown then moves only
    on the paired statistic over at least :attr:`min_dethrone_confirmations`
    shared seeds, and only within the top :attr:`champion_standing_rank`
    official places. There is deliberately no unconditional override.
    """

    crown_duel_mode: Literal["disabled", "fleet_ready"] = "disabled"
    """Whether two bench >= 16 rows dethrone on the sequential title duel
    (protocol 35, ``docs/title-duels.md``).

    ``disabled`` is the protocol-34 gate and the exact rollback: a challenger
    dethrones on one season's paired statistic with the capped fixed-z band.
    ``fleet_ready`` adds ``duel_window_pairs`` to the served crown gate and
    ``duel_history`` to the ledger once every recently-live weight setter
    reports protocol 35, and only while the crown gate itself is served: the
    newest 15 pair-masked ``(season, seed)`` pairs across dueling seasons
    must clear ``max(margin x decay, c(n) x se)``. Close calls keep the king.
    There is deliberately no unconditional override.
    """

    min_dethrone_confirmations: Annotated[
        int, Field(ge=2, le=MAX_STORED_MIN_DETHRONE_CONFIRMATIONS)
    ] = DEFAULT_MIN_DETHRONE_CONFIRMATIONS
    """Shared-seed confirmations a challenger must hold against the champion
    before it may dethrone. Two is the paired statistic's own floor. A new
    write accepts at most :data:`MAX_MIN_DETHRONE_CONFIRMATIONS`; a stored
    revision above it is served clamped (``crown_gate_for``)."""

    champion_standing_rank: Annotated[int, Field(ge=1, le=EMISSION_SET_SIZE)] = (
        DEFAULT_CHAMPION_STANDING_RANK
    )
    """Official places the crown must stay within. A champion that falls below
    it hands the crown to the best-qualified standing challenger."""

    ledger_pin_mode: Literal["live", "epoch"] = "epoch"
    """Whether ``GET /scoring/scores`` serves one pinned ledger per chain epoch.

    ``epoch`` (the default) freezes the fold input once per
    ``SubnetEpochIndex`` so every validator reading during that epoch folds
    byte-identical entries and markers; a ledger change lands for everyone at
    the next pin instead of splitting the fleet on who read first. ``live`` is
    the exact rollback to the time-based read and is a serving change only --
    the fold itself is untouched by this switch, which is why it needs no
    fleet-protocol gate.
    """

    crown_incumbent_mode: Literal["disabled", "fleet_ready"] = "disabled"
    """Whether the KOTH fold defends the crown from the served incumbent.

    ``disabled`` is the historical fold and the exact rollback: every read
    re-derives the champion from the earliest lineage anchor, so a senior
    claimant sitting inside the dethrone band retakes the crown whenever the
    holder's official lead dips under it. ``fleet_ready`` serves the previous
    pin's champion (resolved through its owner family) as ``crown_mode:
    incumbent`` and the fold only moves the crown when a challenger clears the
    band over that incumbent. Like tie weighting it activates only after every
    recently-live weight-setting validator reports the protocol that reads the
    marker, and there is deliberately no unconditional override.
    """

    wave_membership: WaveMembership = DEFAULT_WAVE_MEMBERSHIP
    """Whose retests have to land before a seed counts toward the aggregate.

    **This changes what validators weight, and it is NOT a no-op on merge.**
    ``official_composite`` is the continual mean of the three quorum scores plus
    one score per fold-eligible seed, so widening this widens the estimator,
    re-orders the tail, and moves emission shares.

    It ships ``participants`` -- an authorized, deliberate change to the fold,
    made because ``strict`` is what caused the 03:56Z incident. ``strict``
    remains available as the **rollback path**: one audited revision returns the
    board to the historical behaviour without a redeploy.

    ``strict`` intersects over every *current* emission-set member. That is the
    pre-merge behaviour and it has a defect: a newly finalized agent entering the
    top five with no retests of its own empties the intersection, so every other
    agent's accumulated depth stops counting and ``official_composite`` falls
    back to the three-score quorum median board-wide. The rows are append-only
    and were never deleted, but the fold stops reading them. Every top-five
    membership change discards accumulated retest signal.

    ``participants`` takes the same intersection over members that hold at least
    one confirmation row. An agent at depth zero has never been issued a lease
    for any of these seeds, so it is not protecting one; excluding it can only
    preserve evidence, never fabricate any. Shared-seed comparability is fully
    intact -- every agent receiving a continual mean still shares one intersected
    seed set. **This is the shipped default**: it fixes the incident without
    weakening the property the intersection exists to protect. It also keeps the
    genuinely protective half of ``strict`` -- a member that has started catching
    up still narrows the wave, and only depth *zero* is read as "not in the lane
    yet".

    ``per_agent`` drops the intersection: each agent aggregates over its own
    completed seeds. Most responsive, least comparable -- two agents' means are
    then taken over different seed sets and the difference between them carries
    a seed-composition term that common random numbers exist to cancel. Unbiased
    under exchangeable draws, but noisier, and at a ``KOTH_MARGIN`` of 0.007 the
    added noise is the same size as the decision it feeds.

    See :func:`ditto.db.queries.confirmation_scores.fold_eligible_seeds_by_agent`
    for the full argument. The paired dethrone test is unaffected by all three:
    ``koth._paired_statistic`` computes its own pairwise intersection between
    challenger and champion.
    """

    idle_retests_enabled: bool = False

    wave_cadence_tempos: Annotated[int, Field(ge=0, le=MAX_WAVE_CADENCE_TEMPOS)] = 0
    """Tempo-aligned waves: open new shared seeds only once per N chain epochs.

    ``0`` (the default, and the exact rollback) is the historical pacing: the
    next growth seed opens as soon as the previous wave completes, so the board
    moves whenever validators happen to finish. A positive ``N`` aligns the
    agent competition to the chain: the epoch grid is cut into windows of ``N``
    ``SubnetEpochIndex`` values starting at a multiple of ``N`` (the same grid
    generator rounds use at ``epochs_per_round`` ``N``), and at most
    :attr:`wave_seeds_per_window` NEW seeds open per window. A seed that is
    already open keeps being leased until every member has it, and catch-up of
    recorded coverage is never paced, so a new entrant still converges in one
    round. Only the introduction of fresh shared seeds waits for the next
    window. Crown changes therefore land on a tempo-aligned wave cadence, and
    retest spend is bounded at ``wave_seeds_per_window`` seeds per window.
    """

    wave_seeds_per_window: Annotated[int, Field(ge=1, le=MAX_WAVE_SEEDS_PER_WINDOW)] = (
        DEFAULT_WAVE_SEEDS_PER_WINDOW
    )
    """New shared seeds a tempo-aligned window may open (ignored at cadence 0).

    Three matches the crown gate's ``min_dethrone_confirmations`` default, so
    one window can carry a full dethrone case for a challenger."""

    retest_mode: RetestMode = "continual"
    """How the continual lane spends retests at a dueling bench version (v16+).

    ``continual`` (the default, and the exact rollback) walks every cohort
    member up to the elastic shared-seed target and re-walks on every crown
    change. ``season_duel`` (``docs/title-duels.md``) replaces that walk with
    one title duel per dueling season:

    * the season's family is anchored on the champion that opened the season,
      so a crown change mid-season draws no new seeds and re-walks nobody;
    * the duel is the first :attr:`season_duel_seeds` seeds of that family,
      all opened at once, and every member runs each exactly once;
    * the cohort is the top five (folded and public-board views) only: the tie
      band, :attr:`retest_cohort_size` above five and owner successors are
      ignored, and :attr:`wave_cadence_tempos` does not apply (the season is
      the window).

    It changes only which confirmation seeds are leased, never how evidence
    is folded, so it needs no fleet-protocol gate. Below the dueling floor it
    is inert and the lane behaves exactly as ``continual``.
    """

    season_duel_seeds: Annotated[
        int, Field(ge=MIN_SEASON_DUEL_SEEDS, le=MAX_SEASON_DUEL_SEEDS)
    ] = DEFAULT_SEASON_DUEL_SEEDS
    """Shared seeds per dueling season under ``season_duel`` (ignored
    otherwise). Three matches :attr:`min_dethrone_confirmations`, so one season
    carries a full dethrone case; the run budget is about this many runs per
    top-five member per season."""

    season_duel_rotation_seeds: bool = False
    """Run season rotation re-scores on the season's duel seeds (phase B2).

    Only under ``retest_mode = season_duel`` at a dueling version. Off (the
    default, and the exact rollback) keeps every rotation re-score on its
    validator's canonical seed, so a season costs rotation plus duel runs. On,
    quorum validator ``i`` of a rotating member re-scores on duel seed
    ``i mod season_duel_seeds`` and the accepted score is also recorded as that
    seed's confirmation, so one run is level and duel evidence and the
    continual lane owes nothing for a member whose rotation covers its seeds
    (about half the runs of a season). Only protocol-35 validators, which
    verify a season-scoped seed, are given one; anyone else, or a season whose
    anchor has not pinned within a short grace, keeps the canonical seed. No
    fleet gate: the fold reads ordinary seeds and records either way.
    """

    rollout_standdown: Literal["off", "capable_validators", "all"] = (
        "capable_validators"
    )
    retest_cohort_size: Annotated[
        int, Field(ge=EMISSION_SET_SIZE, le=MAX_RETEST_COHORT_SIZE)
    ] = EMISSION_SET_SIZE
    """How many ranked agents the continual lane may rescore.

    ``5`` (the default) is the historical behaviour: the lane rescores exactly
    the emission set. Above that, the next ranked distinct-miner entrants join
    the same champion-anchored wave, so a challenger arrives at the top five
    with confirmation depth already banked instead of starting from zero.

    Raising this never changes who earns emissions, and never changes when a
    wave completes -- completion stays keyed to the emission set, so the extended
    members cannot stall the crown. They ride each wave seed with whatever
    capacity is left once every emission-set member is claimed.

    This is a **rank** cutoff, which is why :attr:`retest_eligibility_mode`
    exists: a rank cannot express "these two hold the same score".
    """

    retest_eligibility_mode: Literal["fixed", "statistical"] = "fixed"
    """How the bottom edge of the retest cohort is drawn.

    ``fixed`` (the default, and the shipped behaviour) cuts at exactly
    :attr:`retest_cohort_size` by rank. Its defect is that a rank cutoff has no
    way to express a tie: rank *n* is retested continually and rank *n + 1* is
    not, even when the two hold an identical composite and the only thing
    separating them is the fold's ``first_seen`` tiebreak.

    ``statistical`` keeps that same cutoff and then admits anyone below it who is
    **not distinguishable from the agent at the cutoff** at the current sample
    size -- specifically, whose composite is within
    ``retest_eligibility_z * sqrt(se_candidate^2 + se_cutoff^2)`` of it. Three
    properties motivate this over the alternatives:

    * It answers the actual complaint. An exact tie has a gap of zero and is
      admitted at *any* ``z``, including zero.
    * It is the fold's own arithmetic. ``KOTH_DETHRONE_Z`` already decides
      whether a challenger's lead over the champion is real; the same test
      decides here whether the cutoff's lead over a candidate is real. An agent
      that could dethrone the cutoff is one whose ranking is unsettled, which is
      precisely the agent worth spending evidence on.
    * It self-tightens. ``composite_stderr`` shrinks as retests accumulate, so
      the band narrows on its own as the field resolves. A fixed percentile or a
      fixed score delta does the opposite: it stays exactly as wide when the
      measurements get good, and has to be retuned by hand every time the score
      distribution shifts -- which it does on every benchmark version roll.

    The two rejected options: a **top n%** band (the original sketch) is simple
    but still splits a tie when two agents straddle the percentile boundary,
    which is the one thing this is meant to prevent. A fixed **score-gap** delta
    does solve ties, but its units are composite points, so it is not
    scale-free and it ignores how precisely each agent has actually been
    measured.

    Both floor and ceiling still bind: the cohort is never smaller than
    :data:`EMISSION_SET_SIZE` and never larger than
    :attr:`retest_cohort_max_size`.
    """

    retest_eligibility_z: Annotated[
        float, Field(ge=0.0, le=MAX_RETEST_ELIGIBILITY_Z)
    ] = DEFAULT_RETEST_ELIGIBILITY_Z
    """Width of the tie-tolerance band in standard errors. Ignored when
    :attr:`retest_eligibility_mode` is ``fixed``.

    ``0.0`` is a meaningful setting rather than a disabled one: it admits exact
    ties and nothing else, which is the narrowest honest reading of "do not split
    a tie at the boundary".
    """

    retest_cohort_max_size: Annotated[
        int, Field(ge=EMISSION_SET_SIZE, le=MAX_RETEST_COHORT_SIZE)
    ] = MAX_RETEST_COHORT_SIZE
    """Hard ceiling on the cohort once the tie band has been applied.

    An unbounded band could sweep a tightly-packed leaderboard into the retest
    lane and consume the fleet, so the band is always clamped. This is a
    **ceiling, not a target**: with ``fixed`` mode, or with a field that has no
    ties near the cutoff, the cohort stays at
    :attr:`retest_cohort_size` and this never binds.
    """

    @model_validator(mode="after")
    def _check_cohort_bounds(self) -> ContinualRetestSettings:
        if self.retest_cohort_max_size < self.retest_cohort_size:
            raise ValueError(
                f"retest_cohort_max_size ({self.retest_cohort_max_size}) cannot be "
                f"below retest_cohort_size ({self.retest_cohort_size}): the ceiling "
                "would cut into the cohort the fixed rank already admitted"
            )
        return self


class ContinualRetestSettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ContinualRetestSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveContinualRetestSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: ContinualRetestSettings
    checksum: str
    source: Literal["revision", "default"]
    fleet_protocol_ready: bool
    aggregate_active: bool
    tie_weighting_fleet_ready: bool = False
    tie_weighting_active: bool = False
    statistical_band_fleet_ready: bool = False
    statistical_band_active: bool = False
    statistical_band_required_protocol: int = 29
    crown_incumbent_fleet_ready: bool = False
    crown_incumbent_active: bool = False
    crown_incumbent_required_protocol: int = CROWN_INCUMBENT_PROTOCOL
    crown_gate_fleet_ready: bool = False
    crown_gate_active: bool = False
    crown_gate_required_protocol: int = CROWN_GATE_PROTOCOL
    crown_duel_fleet_ready: bool = False
    crown_duel_active: bool = False
    crown_duel_required_protocol: int = CROWN_DUEL_PROTOCOL
    ledger_pin: LedgerPinStatus | None = None
    """The pin validators are folding right now, or ``None`` when no pin has
    ever been taken (bootstrap) or the mode is ``live``."""
    max_age_seconds: float
    open_rollout_desired_version: int | None = None
    rollout_standdown_active: bool = False
    emission_set_size: int = EMISSION_SET_SIZE
    max_retest_cohort_size: int = MAX_RETEST_COHORT_SIZE
    max_retest_eligibility_z: float = MAX_RETEST_ELIGIBILITY_Z
    resolved_cohort_size: int | None = None
    """How many agents the tie band actually admitted on the current board.

    ``retest_cohort_size`` is what the operator asked for; this is what the
    ranking produced once ties at the cutoff were absorbed. They differ only in
    ``statistical`` mode, and the difference is the whole point of the mode, so
    it has to be visible without reading validator logs. ``None`` when it could
    not be computed.
    """
    eligible_agent_count: int | None = None
    """Ranked agents the active generation could supply to the cohort right now.

    The cohort is the smaller of ``retest_cohort_size`` and this, so an operator
    who asks for 25 in a field of nine can see why only nine are being retested.
    ``None`` when the count could not be read.
    """


class AdminContinualRetestSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ContinualRetestSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _check_write_bounds(self) -> AdminContinualRetestSettingsRequest:
        if self.settings.min_dethrone_confirmations > MAX_MIN_DETHRONE_CONFIRMATIONS:
            raise ValueError(
                "min_dethrone_confirmations "
                f"({self.settings.min_dethrone_confirmations}) cannot exceed the "
                f"{MAX_MIN_DETHRONE_CONFIRMATIONS}-seed continual-retest work cap: "
                "a challenger never holds more shared seeds than that"
            )
        if (
            self.settings.retest_mode == "season_duel"
            and self.settings.season_duel_seeds
            < self.settings.min_dethrone_confirmations
        ):
            # The fold pairs only same-season evidence, so a season holding
            # fewer seeds than the crown gate asks for could never move it.
            raise ValueError(
                f"season_duel_seeds ({self.settings.season_duel_seeds}) cannot be "
                "below min_dethrone_confirmations "
                f"({self.settings.min_dethrone_confirmations}): a season duel "
                "would then never carry a full dethrone case"
            )
        return self


class AdminContinualRetestSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[ContinualRetestSettingsRevision]
    history: list[ContinualRetestSettingsRevision]
    default: ContinualRetestSettings
    effective: EffectiveContinualRetestSettings
