"""Versioned, hot-swappable operator policy for the validator queue.

Every constant in here used to be a source-level literal, and retuning one was
a code change plus a review plus a deploy. The queue is retuned *often* --
whenever miner behaviour shifts -- so the tuning itself belongs in the operator
settings system, not in the release process.

These wire models back an append-only revision table, exactly like
``ditto.api_models.continual_retest_settings`` and
``ditto.api_models.screener_review_settings``. A board with no revision behaves
byte-identically to the previously hard-coded values, so shipping this changes
nothing until an operator writes a revision.

Three lifecycles live on one board
==================================

The board is one revision payload (whole policy, never a diff) because that is
what makes a historical revision reconstructable. But the *fields* are consumed
at three different moments, and confusing them is how a queue change becomes an
incident:

``NEXT ROLLOUT`` -- :attr:`~QueuePolicySettings.rescore_cohort_size` and
    :attr:`~QueuePolicySettings.priority_cohort_size`. Read exactly once, when
    an operator starts a rollout, then frozen onto the rollout row
    (``BenchmarkRollout.rescore_cohort_target`` /
    ``.priority_cohort_target``). Editing them never touches an in-flight
    rollout. Safe to write at any time.

``LIVE, ROLLOUT-LOCKED`` -- :attr:`~QueuePolicySettings.lane_cycle_size` and
    :attr:`~QueuePolicySettings.fresh_submission_slots`, listed in
    :data:`ROLLOUT_LOCKED_FIELDS`. Read on every validator job poll, but
    **refused while a rollout is open**. The lane counter is "jobs this
    validator completed since rollout start, mod ``lane_cycle_size``", so
    changing the modulus mid-rollout discontinuously reassigns every validator's
    position in the cycle: a validator that was about to take cohort work
    silently starts taking fresh work instead, and the reverse. The refusal
    costs nothing, because the lane split only *does* anything while a rollout
    is open -- see :func:`rollout_locked_change`.

``LIVE`` -- everything else. Read on the hot path and safe to change at any
    time, because each one only resizes or re-scopes a set rather than moving a
    counter's modulus.

Bounds are spelled out here rather than imported from
``ditto.db.queries.benchmark_rollout``: that module imports this package, so
importing back would be a cycle.
``test_queue_policy_bounds_match_queue_constants`` asserts the two agree, so
drifting them apart fails CI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Cohort sizing bounds
# ---------------------------------------------------------------------------
# Mirrors ``PRIORITY_COHORT_SIZE``: no cohort may be smaller than the priority
# five, which is also the size of the KOTH emission set
# (``MIN_DESIRED_AUTHORITY_AGENTS``). Below five, flipping the ledger to the
# desired version would have fewer recipients than the emission split expects.
MIN_COHORT_SIZE = 5
# Mirrors ``MAX_PERSISTED_RESCORE_COHORT_SIZE`` and the
# ``benchmark_rollout_bounded_members`` CHECK constraint on
# ``benchmark_rollouts.cohort_size``. A larger value could not be persisted.
MAX_COHORT_SIZE = 25
# Mirrors ``DEFAULT_RESCORE_COHORT_SIZE``: the inherited top ten, which is what
# every rollout used before this setting existed.
DEFAULT_RESCORE_COHORT_SIZE = 10
# Mirrors ``PRIORITY_COHORT_SIZE`` as a *default*. The constant survives as the
# floor and as the KOTH top-five; this setting is only the rollout gate.
DEFAULT_PRIORITY_COHORT_SIZE = 5

# ---------------------------------------------------------------------------
# Lane bounds
# ---------------------------------------------------------------------------
# The lane cycle must have at least two slots or there is no split to make.
MIN_LANE_CYCLE_SIZE = 2
# An arbitrary but deliberate ceiling. A modulus above a dozen makes the lane a
# validator visits rarely enough that a small fleet can go a long time without
# ever taking cohort work, which is the starvation this whole mechanism exists
# to prevent -- in the other direction.
MAX_LANE_CYCLE_SIZE = 12
# Mirrors ``_LANE_CYCLE_SIZE`` / ``_FRESH_SUBMISSION_SLOTS`` in
# ``ditto.api_server.endpoints.validator``: three fresh-submission jobs for
# every one rollout-cohort job, per validator.
#
# The default slot set is NOT the contiguous prefix (0, 1, 2). It is (0, 1, 3),
# so the cohort slot sits in the middle of the cycle rather than at its end.
# That is the shipped interleave and it is preserved exactly; deriving the set
# from a count would silently change which poll takes cohort work.
DEFAULT_LANE_CYCLE_SIZE = 4
DEFAULT_FRESH_SUBMISSION_SLOTS = (0, 1, 3)

# ---------------------------------------------------------------------------
# Previous-generation carryover bounds
# ---------------------------------------------------------------------------
MAX_PREV_GEN_CARRYOVER_AGENTS = 50
# The top 25 of the closing generation are the ones the new era offers to adopt,
# matching :data:`MAX_COHORT_SIZE` / the continual board's ``retest_cohort_size``.
# All three answer the same question -- "how deep does the leaderboard stay
# interesting?" -- and an operator who widens one and not the others gets a
# carryover set narrower than the cohort it is meant to feed.
#
# This is a *shipped default*, not a bound, and it is inert on its own: the whole
# carryover path is gated behind ``enabled``, which still ships ``False``. Raising
# it changes what would be adopted **if** an operator turned carryover on; it
# admits nobody by itself. See :attr:`PrevGenCarryoverSettings.enabled` for the
# other gates that also have to move before a single submission is carried over.
DEFAULT_PREV_GEN_CARRYOVER_AGENTS = 25

# ---------------------------------------------------------------------------
# Owner capacity bounds
# ---------------------------------------------------------------------------
# Mirror ``ditto.db.queries.queue_order``'s ceiling constants, which live beside
# the predicate they parameterize. Spelled again here for the same reason the
# cohort bounds are: that module imports this package, so importing back would
# be a cycle. ``test_queue_policy_bounds_match_queue_constants`` asserts they
# agree, so drifting them apart fails CI.
MIN_OWNER_CONCURRENT_SUBMISSIONS = 1
MAX_OWNER_CONCURRENT_SUBMISSIONS = 3
DEFAULT_OWNER_CONCURRENT_SUBMISSIONS = 2

# ---------------------------------------------------------------------------
# Similarity budget bounds
# ---------------------------------------------------------------------------
# Mirror ``ditto.db.queries.queue_order`` and
# ``ditto.db.queries.similarity_grouping`` for the same reason as the owner
# bounds above -- those modules import this package, so importing back would be
# a cycle -- and ``test_queue_policy_bounds_match_queue_constants`` asserts they
# agree.
MIN_SIMILARITY_CONCURRENT_SUBMISSIONS = 1
MAX_SIMILARITY_CONCURRENT_SUBMISSIONS = 3
DEFAULT_SIMILARITY_CONCURRENT_SUBMISSIONS = 1
MIN_SIMILARITY_THRESHOLD_SETTING = 0.70
MAX_SIMILARITY_THRESHOLD_SETTING = 1.0
DEFAULT_SIMILARITY_JACCARD_THRESHOLD = 0.90
DEFAULT_SIMILARITY_CONTAINMENT_THRESHOLD = 0.95

# Fields the queue refuses to change while a benchmark rollout is open, because
# they move the modulus of a counter that is measured from rollout start.
ROLLOUT_LOCKED_FIELDS = ("lane_cycle_size", "fresh_submission_slots")


class DeferredSourceReviewSettings(BaseModel):
    """Hot-swappable post-score deep-review admission and anomaly policy.

    This board decides *where* the expensive source review runs -- before
    scoring (automatic, by a screener), after it (as an operator-adjudicated
    hold), or, in ``bypass``, nowhere at all. Three of the four modes review
    every submission somewhere; ``bypass`` is the one that does not, and it is
    the only way to send admitted submissions straight to validator scoring.

    SCOPE -- source integrity only
    ==============================

    Nothing on this board touches **copy/plagiarism** enforcement. Copy holds
    (``review_kind: "copy"``) are opened from the duplicate-signal decision at
    score finalization, which never reads this board, runs *before* the deferred
    path, and wins outright: a copy hold moves the agent to
    ``ATH_PENDING_REVIEW``, and the deferred path only acts on agents still in
    ``SCORED``/``LIVE``. Setting ``mode="off"`` or ``mode="bypass"`` disables the
    deferred source-integrity branch and **leaves plagiarism detection fully
    armed**. The same is true of the transform/overfit audit, which has its own
    switch. "No source review" never means "no copy detection".
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    mode: Literal["off", "observe", "enforce", "bypass"] = "off"
    """Where the expensive source review runs, and whether it can hold.

    ====================================================================
    mode        pre-score deep screen   post-score qualification   holds
    ====================================================================
    ``off``     every submission        never computed             none
    ``observe`` every submission        computed, recorded only    none
    ``enforce`` build-only admission    computed                   opens
    ``bypass``  build-only admission    never computed             none
    ====================================================================

    Read that table before assuming ``off`` is the light one: ``off`` is the
    *heaviest* mode (a full deep screen on every submission) and ``bypass`` is
    the lightest (no source review anywhere).

    ``off`` -- the legacy full pre-score review. Every fresh submission gets the
        complete deep screen *before* it is scoreable, so no deferred hold is
        ever opened and no post-score qualification is computed. This is the
        heaviest mode for the screener fleet: the deep review runs on every
        submission rather than on the handful that qualify.

    ``observe`` -- pre-score review is the same full deep screen as ``off``, but
        post-score qualification is still computed and written to the append-only
        score audit (``audit_kind: "deferred_source_review"``, ``enforced:
        false``). Nothing is held and the agent proceeds to ``SCORED``. This is
        the mode to pick when turning enforcement off, because it keeps the
        record of which submissions *would* have been held -- rank, composite,
        and the MAD thresholds -- so returning to ``enforce`` later leaves no gap
        in the evidence.

    ``enforce`` -- mechanical-first admission. A fresh submission gets only a
        cheap build-only screen up front (marked
        ``deferred-mechanical-admission``) and is scored immediately; the deep
        review is deferred and runs only for submissions that qualify by
        top-five rank or by exceeding the robust anomaly thresholds. Qualifying
        opens a pending ``deferred_source_review`` hold, moves the agent to
        ``ATH_PENDING_REVIEW``, and excludes it from the emission-eligible
        ledger until the review completes.

    ``bypass`` -- **no source review at all.** Admission is the same cheap
        build-only screen as ``enforce`` (the screened image still has to be
        built, verified and hashed before anything can score it), and no
        post-score qualification is computed, so no deferred hold can ever
        open. An admitted submission goes straight to validator scoring. This
        is the lightest mode and the only one that runs neither half of the
        source review. It does **not** disable copy/plagiarism enforcement --
        see the scope note on the class.

    Flipping back to ``enforce`` later re-qualifies everything that was
    mechanically admitted while ``bypass`` was set: those rows still carry the
    immutable ``deferred-mechanical-admission`` marker and have no terminal deep
    review, so the next canonical ledger mutation reconsiders them. Nothing is
    lost by turning it off, only deferred.

    OPEN HOLDS ALWAYS DRAIN, IN EVERY MODE
    ======================================

    Leaving ``enforce`` stops *new* holds. It deliberately does **not** stop the
    machinery that clears the ones already open: a pending deferred hold is
    resolved by a screener re-claiming the agent's ``ATH_PENDING_REVIEW`` row
    for its deep pass, and that re-claim (``deferred_ath_eligible`` in
    ``ditto.db.queries.screening``) is independent of this field.

    That independence is load-bearing rather than incidental. The re-claim used
    to be gated on ``mode == "enforce"``, which meant any flip away from
    ``enforce`` froze every hold that happened to be open at that instant: those
    agents stayed out of the emission-eligible ledger with no screener willing
    to pick them up again, releasable only one at a time by hand. A control
    whose whole purpose is to reduce review load must not be able to strand
    miners as a side effect, so the gate was removed instead of being inherited
    by ``bypass``. The bounded, finite work of draining an already-open queue is
    the price of that, and it ends.

    Holds are still never auto-cleared: a bulk clearance would write an
    unreasoned resolution onto each agent's public audit record. They drain the
    normal way -- a deep pass with a real verdict, or an explicit
    ``resolve_ath_review``.
    """

    min_cohort_size: Annotated[int, Field(ge=5, le=100)] = 8
    composite_mad_multiplier: Annotated[float, Field(ge=1.0, le=20.0)] = 6.0
    axis_mad_multiplier: Annotated[float, Field(ge=1.0, le=20.0)] = 6.0
    min_composite_delta: Annotated[float, Field(ge=0.0, le=1.0)] = 0.10
    min_axis_delta: Annotated[float, Field(ge=0.0, le=1.0)] = 0.15


def _as_slot_tuple(value: object) -> object:
    """Accept a JSON array for a field that is stored as an immutable tuple.

    JSON has no tuple type, so a strict ``tuple[int, ...]`` would reject every
    real request body. Coercing here keeps the wire shape an array while the
    in-memory value stays immutable, so a resolved policy cannot be mutated by a
    caller that happens to hold a reference to it.
    """
    if isinstance(value, list):
        return tuple(value)
    return value


FreshSubmissionSlots = Annotated[tuple[int, ...], BeforeValidator(_as_slot_tuple)]


class PrevGenCarryoverSettings(BaseModel):
    """When to admit previous-generation submissions that can never finalize.

    Once a new benchmark version activates, nobody will ever issue the third
    prior-version score that a partially-scored prior-generation submission is
    waiting on. Those submissions are not *delayed*, they are permanently
    stranded: the quorum they need cannot be restored by any future event.

    This policy decides which of them the new era adopts. It ships DISABLED, so
    admitting them is always an explicit operator decision with an audit trail.

    The board is slightly wider than its name, because the fleet has two
    previous-generation lanes and they compete for the same slots:

    * :attr:`enabled`, :attr:`max_agents`, :attr:`min_score_count`,
      :attr:`include_exhausted`, :attr:`dedupe_scope` and
      :attr:`require_cohort_complete` shape the adopted carryover.
    * :attr:`require_desired_era_drained` applies to both.

    Neither lane can reach a RETIRED era any more, and no setting on this board
    can put one back. ``allow_retired_era_backfill`` used to live here and did
    exactly that; it is gone, along with the branch that read it. The floor now
    sits in the database (``MIN_SCOREABLE_BENCH_VERSION`` and the constraints
    named there), so what these fields tune is priority WITHIN the scoreable
    versions -- never whether a dead one comes back.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    enabled: bool = False
    """Whether stranded previous-generation submissions are admitted at all.

    Ships ``False``: the entire carryover path is inert until an operator turns
    it on, so this board's arrival is a no-op.
    """

    max_agents: Annotated[int, Field(ge=1, le=MAX_PREV_GEN_CARRYOVER_AGENTS)] = (
        DEFAULT_PREV_GEN_CARRYOVER_AGENTS
    )
    """Hard cap on how many stranded submissions are adopted, ever.

    A cap rather than a rate: carryover is a finite backlog, not a stream, so
    bounding the total is the honest control. Ordering is by demonstrated
    progress first (see :attr:`min_score_count`) and then FIFO, so a cap keeps
    the submissions that are closest to finalizing.

    Defaults to ``25`` -- "the top 25 of the closing generation qualify to enter
    the new era" -- which is the same depth as ``MAX_COHORT_SIZE`` and the
    continual board's ``retest_cohort_size``. Carryover exists to hand the new
    era a populated leaderboard, so adopting fewer than the retest lane will
    later re-benchmark just leaves that lane short of candidates.
    """

    min_score_count: Annotated[int, Field(ge=0, le=2)] = 2
    """Fewest prior-version scores a submission must already have.

    ``2`` (the default) adopts only submissions one score short of quorum. Those
    have *demonstrated they can run* -- two validators already produced a score
    for them -- so adopting them is cheap and very likely to finalize.

    ``0`` also adopts submissions that were never ticketed at all. Those have
    never been executed by any validator, so each one is an open-ended cost with
    no evidence it will ever produce a score. That is a deliberate operator
    choice, not a default.

    The ceiling is 2 because a submission with three prior-version scores is
    finalized and by definition not stranded.
    """

    include_exhausted: bool = False
    """Whether to adopt submissions that burned all their retry attempts.

    Exhausted submissions already consumed their allowance under the prior
    generation. Adopting them hands back capacity that the retry policy
    deliberately took away, so it is off by default.
    """

    dedupe_scope: Literal["coldkey", "hotkey", "none"] = "coldkey"
    """Whose newer submissions suppress their own older stranded ones.

    ``coldkey`` (the default) is the real owner boundary: one miner routinely
    runs several hotkeys, and if they have already submitted something newer
    then their older stranded work is superseded by their own choice and should
    not consume fleet capacity. This matches the owner key the ticket allocator
    itself uses (payment-time coldkey with a hotkey fallback), so carryover and
    allocation agree on who a miner is.

    ``hotkey`` dedupes per registered key instead, which lets one owner carry
    several stranded submissions. ``none`` disables suppression entirely. Both
    are strictly wider than the default.
    """

    require_desired_era_drained: bool = True
    """Whether previous-generation work waits for the desired-era queue to empty.

    ``True`` (the default) makes the previous generation STRICTLY the lowest
    priority thing the fleet does: a retired-era artifact is leased only when no
    admitted desired-era submission has an unfilled quorum slot that any capable
    validator could take. Not a ratio, not a weighted share -- carryover and
    source backfill issue only into a genuinely empty desired-era queue.

    Lane position alone did not deliver that. Both previous-generation lanes
    already sit at the tail of ``request_job``, but reaching them only means
    *this validator's* desired-era lanes came back empty, and they routinely do
    while the queue is deep: one ticket per (agent, version, validator), one
    generation per owner, plus per-validator cooldowns. A validator would take a
    retired-era lease and disappear for its full length while the desired-era
    queue it could not see was still waiting on exactly that slot.

    The gate is fleet-wide but not absolute, and deliberately so. Submissions
    every capable validator is already blocked on -- scored, cooling down,
    retry-exhausted -- do not hold it shut, so a permanently unactionable
    backlog cannot starve the previous generation forever. Setting this
    ``False`` restores the old lane-position-only behaviour, which drains the
    retired era faster at the cost of the desired era's fresh submissions.

    Unlike :attr:`require_cohort_complete`, this applies to BOTH previous-
    generation lanes: the adopted carryover below and the retired-era source
    backfill. They differ in which era's dataset the artifact runs under, not in
    priority -- a miner waiting behind either one is waiting behind the previous
    generation.
    """

    require_cohort_complete: bool = True
    """Whether carryover waits for the inherited rescore cohort to settle.

    ``True`` (the default) confines carryover to genuinely spare cohort-lane
    capacity, exactly like the existing source-backfill path: nothing is adopted
    until the rollout's inherited cohort has finished, so carryover can never
    delay the transition it is riding on. ``False`` interleaves carryover with
    cohort work, which finishes the backlog sooner at the cost of slowing
    activation.
    """


class SimilarityBudgetSettings(BaseModel):
    """How much fleet capacity one *submission* may hold, across all keys.

    The owner ceiling above is keyed on the payment-time coldkey, and an
    operator who spreads one submission across many coldkeys is -- on every
    signal the platform can verify -- many owners. In the production window this
    board was calibrated against, one submission family held 25 of 76 live
    evaluations across 15 hotkeys and 6 coldkeys while honest miners waited.

    This board bounds that directly: near-identical submissions share one
    concurrency budget, whoever paid. It is **queue fairness only**. A grouped
    submission is not held, rejected, quarantined, re-screened or scored
    differently; it waits for the budget to free, exactly as every miner already
    waits behind the owner rail, and it is leased the moment the twin's lease
    ends. Nothing on this board is evidence that anybody copied anything --
    copy enforcement is a separate mechanism with a far higher bar, because
    there a false positive costs a miner their submission rather than a wait.

    The two thresholds are the operator's calibration knob and should be moved
    together with the measurement, not by feel; the module docstring of
    ``ditto.db.queries.similarity_grouping`` records the production
    distributions the shipped values were chosen from.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    enabled: bool = True
    """Whether the similarity rail is consulted at all.

    Ships ``True``, unlike the carryover board, and the difference is
    deliberate: carryover admits work that would otherwise never run, so it must
    be an explicit decision; this rail only *bounds* work that is already
    over-running the fleet, and shipping it off would leave the queue in the
    state that motivated it. ``False`` is the kill switch and it is complete --
    with the rail off no sketch is read and the allocator behaves byte for byte
    as it did before, so an operator who sees an unexpected grouping can undo it
    in one revision without a deploy.
    """

    concurrent_submission_limit: Annotated[
        int,
        Field(
            ge=MIN_SIMILARITY_CONCURRENT_SUBMISSIONS,
            le=MAX_SIMILARITY_CONCURRENT_SUBMISSIONS,
        ),
    ] = DEFAULT_SIMILARITY_CONCURRENT_SUBMISSIONS
    """How many near-identical submissions may hold live leases at once.

    Ships at ``1``: one copy of a submission runs at a time. Tighter than the
    owner ceiling's ``2`` on purpose -- the owner ceiling relaxes to fill idle
    slots, and idle slots filled by twenty copies of one submission is the exact
    outcome this rail exists to prevent, so it does not relax. Raising it to
    ``2`` or ``3`` widens the budget uniformly for every family.

    The unit is submissions, not leases, matching both owner rails. One
    submission can occupy at most a full quorum, and filling a submission's own
    quorum is never similarity contention: three validators scoring one row is a
    completed evaluation, not a monopoly.
    """

    jaccard_threshold: Annotated[
        float,
        Field(ge=MIN_SIMILARITY_THRESHOLD_SETTING, le=MAX_SIMILARITY_THRESHOLD_SETTING),
    ] = DEFAULT_SIMILARITY_JACCARD_THRESHOLD
    """Symmetric-overlap threshold on the miner-authored residual.

    The primary channel. ``0.90`` ships because it is the widest value measured
    to be clean: at ``0.90`` zero of 13,464 production pairs drawn from
    independent miners are grouped, and the family this board targets still
    collapses to a single budget. Lowering it to ``0.85`` admits a false pair
    and merges nothing new.

    The floor of ``0.70`` is not a style choice. Independent production miners
    reach a 99.9th percentile of 0.625 on this metric, so a threshold beneath
    the floor would configure exactly the false grouping the rail must avoid --
    and a falsely grouped honest miner silently loses half their throughput.
    """

    containment_threshold: Annotated[
        float,
        Field(ge=MIN_SIMILARITY_THRESHOLD_SETTING, le=MAX_SIMILARITY_THRESHOLD_SETTING),
    ] = DEFAULT_SIMILARITY_CONTAINMENT_THRESHOLD
    """Asymmetric-overlap threshold, for a resubmission padded with extra files.

    Padding dilutes Jaccard while leaving containment near 1.0, so this is the
    channel that keeps the rail from being defeated by adding junk. It sits
    higher than the Jaccard threshold (``0.95``) because it is the looser
    measure: independent production miners reach 0.941 on it, against 0.891 on
    Jaccard. A scale guard in the grouping module keeps its degenerate case --
    a small residual sitting inside a large one -- from ever reaching here.
    """


class QueuePolicySettings(BaseModel):
    """The complete, hot-swappable validator-queue policy.

    Each revision stores the whole policy (not a diff), so a frozen snapshot is
    always reconstructable and a read never merges partial revisions.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    # -- NEXT ROLLOUT (frozen onto the rollout row at start) ----------------

    rescore_cohort_size: Annotated[
        int, Field(ge=MIN_COHORT_SIZE, le=MAX_COHORT_SIZE)
    ] = DEFAULT_RESCORE_COHORT_SIZE
    """How many inherited agents the *next* rollout re-scores on its target
    version (``5 <= n <= 25``). Frozen onto the rollout row at start."""

    priority_cohort_size: Annotated[
        int, Field(ge=MIN_COHORT_SIZE, le=MAX_COHORT_SIZE)
    ] = DEFAULT_PRIORITY_COHORT_SIZE
    """How many of the inherited cohort's top positions gate activation.

    The rollout may not take ledger authority until every one of the top
    ``priority_cohort_size`` inherited agents holds a complete target-version
    quorum. Raising it makes activation strictly more conservative -- and
    strictly more stallable, since one unqualifiable agent inside the widened
    band now blocks the transition. It can never go below
    :data:`MIN_COHORT_SIZE`, which is the KOTH emission-set size.

    Frozen onto the rollout row at start, so raising it never re-gates a
    transition already in flight.
    """

    # -- LIVE, ROLLOUT-LOCKED ---------------------------------------------

    lane_cycle_size: Annotated[
        int, Field(ge=MIN_LANE_CYCLE_SIZE, le=MAX_LANE_CYCLE_SIZE)
    ] = DEFAULT_LANE_CYCLE_SIZE
    """Length of the per-validator lane rotation.

    Each validator counts the jobs it has completed since rollout start and
    takes its lane from ``count % lane_cycle_size``. Changing this while a
    rollout is open is refused; see :data:`ROLLOUT_LOCKED_FIELDS`.
    """

    fresh_submission_slots: FreshSubmissionSlots = DEFAULT_FRESH_SUBMISSION_SLOTS
    """Which slots of the cycle serve new submissions rather than the cohort.

    Defaults to ``(0, 1, 3)`` of a 4-slot cycle: three fresh-submission jobs per
    one rollout-cohort job, with the cohort slot in the middle. Both lanes have
    a floor of one slot -- an empty fresh lane starves new miners (the failure
    this split was written to prevent), and an empty cohort lane means the
    rollout never reaches quorum and can never activate.
    """

    # -- LIVE -------------------------------------------------------------
    #
    # ``PROVISIONAL_CONTENDER_LANE_SIZE`` (the 10-agent fast lane in
    # ``ditto.db.queries.tickets``) is a deliberate omission, not an oversight.
    # It is genuinely queue policy and it has been retuned before, but its value
    # feeds three consumers -- the ``.limit()`` on the contender subquery, the
    # tenth-place floor in ``get_score_priority_floors``, and the public
    # ``validator_queue_rank`` preview -- and that preview is already known to
    # disagree with the allocator about who a miner is (it groups by hotkey
    # where the allocator groups by payment-time coldkey). Making the size
    # operator-tunable before that divergence is fixed would let one setting
    # produce two different lanes. It is tracked separately.

    owner_concurrent_submission_limit: Annotated[
        int,
        Field(ge=MIN_OWNER_CONCURRENT_SUBMISSIONS, le=MAX_OWNER_CONCURRENT_SUBMISSIONS),
    ] = DEFAULT_OWNER_CONCURRENT_SUBMISSIONS
    """How many of one owner's submissions may hold live leases at once.

    Applies **only** to the allocator's last-resort pass. The ordinary pass is
    untouched at any value: one live submission per owner fleet-wide, pinned to
    one generation, ordered by the queue the whole fleet shares. This number
    decides what happens after a validator has walked its entire candidate set
    and found nothing it may lease -- the moment when the alternative is not
    "serve someone else" but "serve nobody and idle the slot".

    ``1`` disables the second pass and restores strict serialization exactly.
    It ships at ``2`` rather than ``1`` deliberately: a knob whose default is
    the old behaviour changes nothing, and the board's own history is that
    empty defaults are how a fix ships and never takes effect. Two is the
    smallest value that is not "off".

    The unit is submissions, not leases, because that is what both owner rails
    already speak in. One submission can occupy at most ``SCORING_QUORUM``
    slots, so the arithmetic worst case for an owner is ``limit * 3`` -- and
    only while no other owner has a single eligible submission anywhere. The
    ceiling of 3 exists so that even that worst case cannot be raised past the
    point where one owner could hold a small fleet for a full lease.
    """

    similarity_budget: SimilarityBudgetSettings = SimilarityBudgetSettings()
    """How much fleet capacity one submission may hold across every key."""

    prev_gen_carryover: PrevGenCarryoverSettings = PrevGenCarryoverSettings()
    """Adoption policy for stranded previous-generation submissions."""

    deferred_source_review: DeferredSourceReviewSettings = (
        DeferredSourceReviewSettings()
    )
    """Mechanical-first admission and post-score deep-review policy.

    Top-five qualification is a fixed integrity invariant in ``enforce`` mode;
    operators can tune only the robust anomaly trigger or roll the whole feature
    between off, observe, and enforce.
    """

    @model_validator(mode="after")
    def _check_coherent(self) -> QueuePolicySettings:
        """Reject combinations that individually validate but jointly deadlock.

        Field-level ranges cannot express these: each one is a relation between
        two settings, and getting any of them wrong wedges the queue rather than
        merely mistuning it.
        """
        if self.priority_cohort_size > self.rescore_cohort_size:
            raise ValueError(
                "priority_cohort_size "
                f"({self.priority_cohort_size}) cannot exceed rescore_cohort_size "
                f"({self.rescore_cohort_size}): activation would wait on inherited "
                "positions the cohort never fills, so the rollout could never "
                "activate"
            )
        slots = self.fresh_submission_slots
        if len(set(slots)) != len(slots):
            raise ValueError(
                f"fresh_submission_slots must be unique, got {list(slots)}"
            )
        if not slots:
            raise ValueError(
                "fresh_submission_slots cannot be empty: every validator poll "
                "would serve the rollout cohort and new submissions would never "
                "be scored, which is exactly the miner starvation the lane split "
                "exists to prevent"
            )
        out_of_range = [s for s in slots if not 0 <= s < self.lane_cycle_size]
        if out_of_range:
            raise ValueError(
                f"fresh_submission_slots {out_of_range} fall outside the cycle "
                f"[0, {self.lane_cycle_size}): those slots could never come up, so "
                "the fresh-submission lane would be smaller than it looks"
            )
        if len(slots) >= self.lane_cycle_size:
            raise ValueError(
                f"fresh_submission_slots {list(slots)} fills the whole "
                f"{self.lane_cycle_size}-slot cycle: no slot would ever serve the "
                "rollout cohort, so an open rollout could never reach quorum or "
                "activate"
            )
        return self

    @property
    def sorted_fresh_submission_slots(self) -> tuple[int, ...]:
        """Slots in ascending order, for stable display and checksums."""
        return tuple(sorted(self.fresh_submission_slots))

    def fresh_submission_lane_due(self, completed_jobs: int) -> bool:
        """Whether a validator's next job comes from the fresh-submission lane.

        The single authority for the lane decision, so the endpoint and any
        projection cannot drift apart the way the queue-rank preview did.
        """
        return completed_jobs % self.lane_cycle_size in set(self.fresh_submission_slots)


def rollout_locked_change(
    current: QueuePolicySettings, proposed: QueuePolicySettings
) -> tuple[str, ...]:
    """Which rollout-locked fields differ between two policies.

    Returns the offending field names, or an empty tuple when the proposal
    leaves every locked field alone. Callers refuse a non-empty result while a
    rollout is open, which lets an operator still retune everything else --
    cohort sizes, the contender lane, carryover -- mid-rollout.
    """
    changed: list[str] = []
    for field in ROLLOUT_LOCKED_FIELDS:
        left = getattr(current, field)
        right = getattr(proposed, field)
        if field == "fresh_submission_slots":
            left, right = tuple(sorted(left)), tuple(sorted(right))
        if left != right:
            changed.append(field)
    return tuple(changed)


class QueuePolicySettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    scope: str
    settings: QueuePolicySettings
    reason: str
    actor: str
    created_at: datetime
    checksum: str


class EffectiveQueuePolicySettings(BaseModel):
    """What the queue is using now, plus what an open rollout already froze."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    scope: str
    settings: QueuePolicySettings
    checksum: str
    source: Literal["revision", "default"]
    min_cohort_size: int = MIN_COHORT_SIZE
    max_cohort_size: int = MAX_COHORT_SIZE
    rollout_locked_fields: tuple[str, ...] = ROLLOUT_LOCKED_FIELDS
    """Fields that cannot be changed while a rollout is open."""
    rollout_is_open: bool = False
    """Whether the locked fields are currently frozen."""
    open_rollout_desired_version: int | None = None
    """Target version of the open rollout, if any."""
    open_rollout_rescore_cohort_target: int | None = None
    """The rescore size that rollout froze at start. Immune to later revisions."""
    open_rollout_priority_cohort_target: int | None = None
    """The priority size that rollout froze at start. Immune to later revisions."""
    open_rollout_overrides_setting: bool = False
    """True when an open rollout froze a cohort size other than the configured
    one, so the operator can see the new value only applies to the next
    rollout."""


class AdminQueuePolicySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    scope: str = "*"
    expected_revision: Annotated[int, Field(ge=0)]
    settings: QueuePolicySettings
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @model_validator(mode="after")
    def _require_complete_policy(self) -> AdminQueuePolicySettingsRequest:
        """Reject a partial policy on a whole-object store.

        :class:`QueuePolicySettings` has a default for every field, which is what
        makes a board with no revision behave like the old hard-coded queue. On a
        *write* that same convenience is a footgun: a revision stores the whole
        policy, so an operator who sends only ``{"rescore_cohort_size": 25}``
        would silently reset every other knob to its shipped default -- turning
        off a carryover gate they had enabled, or reverting a lane split -- while
        believing they changed one number.

        ``expected_revision`` cannot catch this: the operator holds the current
        revision, they just under-specified the body. So require every field to
        be present explicitly and name the missing ones.
        """
        missing = sorted(
            set(QueuePolicySettings.model_fields) - self.settings.model_fields_set
        )
        if missing:
            raise ValueError(
                "a queue policy revision stores the WHOLE policy, so every field "
                "must be sent explicitly; missing "
                f"{missing}. Read GET /admin/queue-policy-settings, change the "
                "fields you want, and send back the complete object."
            )
        carryover_missing = sorted(
            set(PrevGenCarryoverSettings.model_fields)
            - self.settings.prev_gen_carryover.model_fields_set
        )
        if carryover_missing:
            raise ValueError(
                f"prev_gen_carryover is stored whole too; missing {carryover_missing}"
            )
        similarity_missing = sorted(
            set(SimilarityBudgetSettings.model_fields)
            - self.settings.similarity_budget.model_fields_set
        )
        if similarity_missing:
            raise ValueError(
                f"similarity_budget is stored whole too; missing {similarity_missing}"
            )
        deferred_missing = sorted(
            set(DeferredSourceReviewSettings.model_fields)
            - self.settings.deferred_source_review.model_fields_set
        )
        if deferred_missing:
            raise ValueError(
                "deferred_source_review is stored whole too; missing "
                f"{deferred_missing}"
            )
        return self


class AdminQueuePolicySettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: list[QueuePolicySettingsRevision]
    history: list[QueuePolicySettingsRevision]
    default: QueuePolicySettings
    effective: EffectiveQueuePolicySettings
