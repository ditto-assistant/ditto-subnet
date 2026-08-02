"""Hot-swappable operator policy for continual top-five retests."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The KOTH emission set is five (champion + four-entry participation tail) and is
# frozen consensus shared with ``ditto-subnet``'s weight fold. It is the floor of
# the retest cohort, never a value the operator can lower: the shared-seed wave
# exists to keep exactly those five comparable, so a cohort that excluded one of
# them would stop the lane doing its only consensus-relevant job.
EMISSION_SET_SIZE = 5

WaveMembership = Literal["strict", "participants", "per_agent"]

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
DEFAULT_RETEST_ELIGIBILITY_Z = 1.64


class ContinualRetestSettings(BaseModel):
    """Complete subnet-global continual-retest policy stored per revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    aggregate_mode: Literal["disabled", "fleet_ready", "enabled"] = "fleet_ready"

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
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: ContinualRetestSettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveContinualRetestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    scope: str
    settings: ContinualRetestSettings
    checksum: str
    source: Literal["revision", "default"]
    fleet_protocol_ready: bool
    aggregate_active: bool
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
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: ContinualRetestSettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


class AdminContinualRetestSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current: list[ContinualRetestSettingsRevision]
    history: list[ContinualRetestSettingsRevision]
    default: ContinualRetestSettings
    effective: EffectiveContinualRetestSettings
