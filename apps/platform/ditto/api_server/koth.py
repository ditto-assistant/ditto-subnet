"""Read-only projection of the validator's frozen KOTH emissions fold.

The canonical consensus implementation lives in ``ditto-subnet`` at
``ditto/validator/weights.py``.  The platform uses this small, pure projection
only to explain that fold on the public leaderboard; validators still compute
and submit their own weights.  Keep the constants and comparison semantics
byte-for-byte aligned with the subnet implementation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from ditto.api_server.efficiency import (
    CURVE_VERSION_BOUNDED_FACTOR,
    CURVE_VERSION_UNBOUNDED_FACTOR,
    is_factor_curve,
)
from ditto.score_order import rank_submissions

# Frozen consensus constants from ditto-subnet/ditto/validator/config.py.
KOTH_MARGIN = 0.007
# Keep these values byte-for-byte aligned with ditto-subnet's consensus fold.
# Bench v6+ shrinks the whole band; legacy/mixed comparisons remain unchanged.
KOTH_BAND_DECAY_MIN_BENCH_VERSION = 6
KOTH_BAND_DECAY_START_COMPOSITE = 0.60
KOTH_BAND_DECAY_RATE = 2.0
# Protocol 24: the decay above is open-loop and, on a saturated benchmark, can
# require more score than the challenger can attain at all. The band is then
# also capped at this share of the remaining headroom, keeping the required
# dethrone score strictly below the challenger's ceiling.
KOTH_CEILING_HEADROOM_SHARE = 0.5
KOTH_TAIL_SIZE = 4
KOTH_RANK_SHARES = (0.65, 0.14, 0.10, 0.07, 0.04)
KOTH_CHAMPION_SHARE = KOTH_RANK_SHARES[0]
KOTH_DETHRONE_Z = 1.64

# One tempo = 360 blocks (~72 min at 12 s/block); mirrors the subnet worker's
# rescore cadence.  The top-5 continual shared-seed rescore lane opens rounds on
# a reign-backoff over the champion's crown (see ``top5_round_is_due``).
BLOCKS_PER_TEMPO = 360

# Continual retests gather enough shared-seed evidence to make the one-sided
# 95% uncertainty half-width no wider than the fixed KOTH margin. Eight is the
# minimum credible variance sample; 32 is the fleet-work hard stop when current
# variance would otherwise ask for an impractical number of runs.
TOP5_MIN_CONFIRMATION_SEEDS = 8
TOP5_MAX_CONFIRMATION_SEEDS = 32


@dataclass(frozen=True)
class KothEntry:
    """The public-safe subset of one active ledger row used by the fold."""

    miner_hotkey: str
    agent_id: UUID
    composite: float
    first_seen: datetime
    raw_rank: int
    bench_version: int = 1
    composite_stderr: float | None = None
    quorum_composites: tuple[float, ...] | None = None
    completed_wave_composites: tuple[float, ...] | None = None
    confirmation_composites: tuple[float, ...] | None = None
    confirmation_seeds: tuple[int, ...] | None = None
    efficiency_bonus: float | None = None
    efficiency_factor: float | None = None
    efficiency_curve_version: int | None = None


@dataclass(frozen=True)
class DethroneDecision:
    """Why the raw score leader did or did not clear the incumbent."""

    challenger_lead: float
    required_lead: float
    margin_lead: float
    statistical_lead: float | None
    method: Literal["flat", "unpaired", "paired"]
    dethrones: bool
    required_score: float
    score_ceiling: float
    ceiling_deadlocked: bool


@dataclass(frozen=True)
class KothProjection:
    champion: KothEntry
    tail: tuple[KothEntry, ...]
    raw_leader: KothEntry
    raw_leader_decision: DethroneDecision | None


@dataclass(frozen=True)
class EmissionAllocation:
    """Projected recipients and shares for one active validator fold."""

    mode: Literal["ranked", "score_ceiling_pool"]
    members: tuple[KothEntry, ...]
    shares: tuple[float, ...]


def _dethrone_band_scale(
    challenger: KothEntry, champion: KothEntry, champion_composite: float
) -> float:
    """Mirror the validator's versioned high-score indifference-band decay."""
    comparison_version = min(challenger.bench_version, champion.bench_version)
    if comparison_version < KOTH_BAND_DECAY_MIN_BENCH_VERSION:
        return 1.0
    bounded_champion = min(
        max(champion_composite, KOTH_BAND_DECAY_START_COMPOSITE), 1.0
    )
    return math.exp(
        -KOTH_BAND_DECAY_RATE * (bounded_champion - KOTH_BAND_DECAY_START_COMPOSITE)
    )


def _ceiling_capped_band(
    band: float,
    challenger: KothEntry,
    champion: KothEntry,
    champion_score: float,
    *,
    active: bool,
) -> float:
    """Mirror the validator's ceiling-aware cap on the dethrone band."""
    if not active:
        return band
    comparison_version = min(challenger.bench_version, champion.bench_version)
    if comparison_version < KOTH_BAND_DECAY_MIN_BENCH_VERSION:
        return band
    headroom = _effective_score_ceiling(challenger) - champion_score
    if headroom <= 0.0:
        return 0.0
    return min(band, KOTH_CEILING_HEADROOM_SHARE * headroom)


def emission_set(projection: KothProjection | None) -> tuple[KothEntry, ...]:
    """Return the emission set (champion + up to 4 distinct-miner tail = top 5).

    This is the membership of the continual top-5 shared-seed rescore lane.  It
    reuses the frozen KOTH fold (:func:`project_koth`): the champion via the
    paired dethrone chain, the tail via ``project_koth``'s
    ``KOTH_TAIL_SIZE``-capped, distinct-miner ``-composite`` ordering.  The
    champion is always first (the anchor), followed by the tail in fold order.
    A newcomer that enters the top 5 automatically joins the set; one that drops
    out stops -- membership follows the set, no manual list.

    The result contains no duplicate ``agent_id`` (``project_koth`` already
    excludes the champion's miner from the tail), so it is at most five entries.
    """
    if projection is None:
        return ()
    seen = {projection.champion.agent_id}
    members = [projection.champion]
    for entry in projection.tail:
        if entry.agent_id in seen:
            continue
        seen.add(entry.agent_id)
        members.append(entry)
    return tuple(members)


def emission_shares(
    projection: KothProjection | None, *, tie_pooling: bool = False
) -> tuple[float, ...]:
    """Return occupied rank shares, optionally pooled across evidence ties."""
    members = emission_set(projection)
    shares = list(KOTH_RANK_SHARES[: len(members)])
    if not tie_pooling:
        return tuple(shares)
    start = 0
    while start < len(members):
        anchor = members[start]
        end = start + 1
        while end < len(members) and _weight_tied(members[end], anchor):
            end += 1
        if end - start > 1:
            average = sum(shares[start:end]) / (end - start)
            shares[start:end] = [average] * (end - start)
        start = end
    return tuple(shares)


def emission_allocation(
    entries: Sequence[KothEntry],
    projection: KothProjection | None,
    *,
    tie_pooling: bool = False,
    ceiling_band_clamp: bool = False,
) -> EmissionAllocation:
    """Return the exact validator payout mode, membership, and shares.

    Normal operation uses the historical champion-plus-tail schedule, with
    evidence-tied occupied slots pooled when enabled. If the best challenger
    cannot mathematically clear the incumbent before reaching its frozen score
    ceiling, the highest evidence-tied cohort becomes an uncapped joint crown
    and splits the full miner pool equally.
    """
    if projection is None:
        return EmissionAllocation(mode="ranked", members=(), shares=())
    if tie_pooling:
        ceiling_cohort = _score_ceiling_cohort(
            entries, projection, ceiling_band_clamp=ceiling_band_clamp
        )
        if ceiling_cohort:
            share = 1.0 / len(ceiling_cohort)
            return EmissionAllocation(
                mode="score_ceiling_pool",
                members=ceiling_cohort,
                shares=(share,) * len(ceiling_cohort),
            )
    return EmissionAllocation(
        mode="ranked",
        members=emission_set(projection),
        shares=emission_shares(projection, tie_pooling=tie_pooling),
    )


def _score_ceiling_cohort(
    entries: Sequence[KothEntry],
    projection: KothProjection,
    *,
    ceiling_band_clamp: bool = False,
) -> tuple[KothEntry, ...]:
    # Curve-v3 protocol 21 has no continuous adjusted-score ceiling: quality is
    # the primary order and efficiency only breaks an exact quality tie.
    if _quality_primary_efficiency_active(entries):
        return ()
    ranked = _distinct_ranked(entries)
    challenger = next(
        (
            entry
            for entry in ranked
            if entry.miner_hotkey != projection.champion.miner_hotkey
        ),
        None,
    )
    if challenger is None:
        return ()
    decision = _dethrone_decision(
        challenger, projection.champion, ceiling_band_clamp=ceiling_band_clamp
    )
    if not decision.ceiling_deadlocked:
        return ()

    anchor = ranked[0]
    cohort = [anchor]
    for entry in ranked[1:]:
        if not _weight_tied(entry, anchor):
            break
        cohort.append(entry)
    return tuple(cohort) if len(cohort) > 1 else ()


def champion_defense(
    entries: Sequence[KothEntry],
    projection: KothProjection | None,
    *,
    ceiling_band_clamp: bool = False,
) -> DethroneDecision | None:
    """What the best rival miner currently needs to take the crown.

    :attr:`KothProjection.raw_leader_decision` answers this only while the raw
    leader is somebody *other* than the champion; the moment the champion is
    also the raw leader it goes ``None`` and the board stops explaining the
    requirement at exactly the point challengers most want it. That is not a
    quiet gap: it hides :attr:`DethroneDecision.ceiling_deadlocked`, the state
    where ``required_score`` has climbed past ``score_ceiling`` and no
    submission can dethrone the incumbent at any score.

    Same comparison the fold runs, against the same highest-ranked entry from a
    different miner, so this never disagrees with the fold about who could win.
    ``None`` only when there is no rival miner to compare.
    """
    if projection is None:
        return None
    challenger = next(
        (
            entry
            for entry in _distinct_ranked(entries)
            if entry.miner_hotkey != projection.champion.miner_hotkey
        ),
        None,
    )
    if challenger is None:
        return None
    return _dethrone_decision(
        challenger, projection.champion, ceiling_band_clamp=ceiling_band_clamp
    )


def _distinct_ranked(entries: Sequence[KothEntry]) -> tuple[KothEntry, ...]:
    ranked = _ranked_entries(entry for entry in entries if entry.composite > 0.0)
    distinct: list[KothEntry] = []
    seen_hotkeys: set[str] = set()
    for entry in ranked:
        if entry.miner_hotkey in seen_hotkeys:
            continue
        seen_hotkeys.add(entry.miner_hotkey)
        distinct.append(entry)
    return tuple(distinct)


def indistinguishable_from(
    candidate: KothEntry, cutoff: KothEntry, *, tolerance_z: float
) -> bool:
    """Whether ``candidate`` is statistically tied with the ``cutoff`` agent.

    The tolerance is the same unpaired two-sample band the dethrone decision
    uses (:func:`_dethrone_decision`'s ``unpaired`` branch):
    ``z * sqrt(se_candidate^2 + se_cutoff^2)``. Reusing it is the point --- an
    agent that could statistically dethrone the cutoff is exactly an agent whose
    ranking against the cutoff is not yet settled, and therefore exactly the
    agent more evidence should be spent on.

    A missing or invalid stderr contributes zero rather than disqualifying the
    comparison, so the degenerate case (no stderr anywhere, ``tolerance_z`` of
    zero) still admits an **exact** tie. That is the case that motivated this:
    rank 11 holding the identical composite to rank 10 is not a ranking, it is a
    coin flip, and a fixed cutoff resolves it by arbitrary tiebreak.
    """
    quality_primary = _quality_primary_efficiency_active((candidate, cutoff))
    score = continual_composite if quality_primary else effective_composite
    gap = score(cutoff) - score(candidate)
    if gap <= 0.0:
        return True
    # Stderr lives on the pre-efficiency quality scale. Propagate it through
    # the frozen score transform before deciding whether the cutoff is
    # unsettled, matching both Platform's dethrone decision and the validator
    # fold.
    candidate_stderr = (_stderr(candidate) or 0.0) * (
        1.0 if quality_primary else _efficiency_stderr_scale(candidate)
    )
    cutoff_stderr = (_stderr(cutoff) or 0.0) * (
        1.0 if quality_primary else _efficiency_stderr_scale(cutoff)
    )
    tolerance = tolerance_z * math.sqrt(candidate_stderr**2 + cutoff_stderr**2)
    return gap <= tolerance


def retest_cohort(
    entries: Sequence[KothEntry],
    projection: KothProjection | None,
    *,
    size: int,
    max_size: int | None = None,
    tolerance_z: float = 0.0,
) -> tuple[KothEntry, ...]:
    """Return the continual-retest cohort: the top ``size`` ranked agents.

    ``size == EMISSION_SET_SIZE`` reproduces :func:`emission_set` exactly --- same
    champion anchor, same ``-effective_composite`` distinct-miner ordering, same
    membership --- because this reuses ``project_koth``'s tail rule and only
    raises the cap it applies. That equality is the point: the operator dial
    starts from the historical lane and extends it, so nothing about the top five
    changes when it moves.

    Above five, the next ranked entrants join the cohort. They are rescored on
    the same champion-anchored wave seeds, which is what makes a challenger's
    arrival in the emission set cheap: it brings confirmation depth with it
    instead of needing a fresh sweep before it can settle a paired comparison.

    ``entries`` must be the same pool ``projection`` was built from; the
    champion comes from the projection's dethrone chain, never from rank order.

    Tie-tolerant extension
    ======================

    ``size`` alone is a **rank** cutoff, and a rank cutoff has no way to express
    "these two are the same score". It will admit rank ``size`` and refuse rank
    ``size + 1`` even when the two hold an identical composite and the only thing
    separating them is :func:`project_koth`'s ``first_seen`` tiebreak. That is
    both unfair and statistically empty.

    ``max_size`` opens a tie-tolerant band below the cutoff: after the fixed
    ``size`` members are taken, any further-ranked agent that is not
    distinguishable from the **last included member** (see
    :func:`indistinguishable_from`) also joins, up to ``max_size`` total. The
    band is anchored on the cutoff agent and not walked transitively, so it
    cannot chain down the whole leaderboard one indistinguishable step at a time.

    ``max_size is None`` (or ``<= size``) disables the band entirely and returns
    byte-identically to the fixed-rank behaviour, which is what ships.
    """
    if projection is None:
        return ()
    champion = projection.champion
    ranked = _ranked_entries(
        entry
        for entry in entries
        if entry.composite > 0.0 and entry.miner_hotkey != champion.miner_hotkey
    )
    base_size = max(1, size)
    ceiling = base_size if max_size is None else max(base_size, max_size)
    seen = {champion.agent_id}
    members = [champion]
    # The cutoff is the last member the FIXED rank would have admitted. It is
    # captured before the band opens so that every extension is measured against
    # the same reference; measuring against the running last member would let the
    # cohort creep down the board one indistinguishable pair at a time.
    cutoff: KothEntry | None = None
    for entry in ranked:
        if entry.agent_id in seen:
            continue
        if len(members) >= ceiling:
            break
        if len(members) >= base_size and (
            cutoff is None
            or not indistinguishable_from(entry, cutoff, tolerance_z=tolerance_z)
        ):
            break
        seen.add(entry.agent_id)
        members.append(entry)
        if len(members) == base_size:
            cutoff = entry
    return tuple(members)


def tempo_index(block_number: int) -> int:
    """The tempo ordinal a chain block falls in (``block // BLOCKS_PER_TEMPO``)."""
    return block_number // BLOCKS_PER_TEMPO


def top5_round_is_due(
    current_block: int,
    crown_block: int,
    *,
    base: int,
    doubling_k: int,
    cap: int,
) -> bool:
    """Whether a top-5 shared-seed rescore round is due at ``current_block``.

    The interval between rounds is an **exponential backoff over the champion's
    reign** (``docs/top5-rescore-lane.md`` §4): dense while a fresh or contested
    king must prove its crown on many seeds, sparse once the reign settles ---
    saving tokens on a stable leader. Measured in tempos since the champion's
    ``crown_block`` (a deterministic ledger fact that changes on any king
    change, so churn re-enters the dense regime and stagnation tapers)::

        interval(reign_tempos) = min(base * 2**floor(reign_tempos / K), cap)

    A round is due exactly when the current reign-tempo lands on a scheduled
    point of that growing schedule (offset 0 = the crown tempo, then repeatedly
    advancing by the interval at each reached point). ``base`` holds for the
    first ``doubling_k`` reign-tempos, front-loading the densest rounds across
    the ~24 h king-source-reveal window (#277/#278) before doubling begins. The
    interval is capped, so the rate never reaches zero -- a champion flatlining
    at ``cap`` is itself the "field has gone stagnant" signal.

    Pure and deterministic: a function only of the two block numbers and the
    consensus constants, so every validator hitting the platform at the same
    height gets the same decision. ``base <= 0`` disables the lane.
    """
    if base <= 0:
        return False
    step_cap = max(base, cap)
    span = max(1, doubling_k)
    reign_tempo = max(0, current_block - crown_block) // BLOCKS_PER_TEMPO
    scheduled = 0
    while scheduled < reign_tempo:
        interval = min(base * (2 ** (scheduled // span)), step_cap)
        scheduled += interval
    return scheduled == reign_tempo


def _quality_primary_efficiency_active(entries: Iterable[KothEntry]) -> bool:
    """Whether protocol-21 curve-v3 ordering is present in this ledger."""
    return any(_bounded_efficiency_factor(entry) is not None for entry in entries)


def _ranked_entries(entries: Iterable[KothEntry]) -> list[KothEntry]:
    """Rank quality first and efficiency only inside an exact quality tier."""
    materialized = list(entries)
    if not _quality_primary_efficiency_active(materialized):
        return rank_submissions(
            materialized,
            scores={
                entry.agent_id: effective_composite(entry) for entry in materialized
            },
        )
    quality = {entry.agent_id: continual_composite(entry) for entry in materialized}
    efficiency = {entry.agent_id: effective_composite(entry) for entry in materialized}
    return rank_submissions(materialized, scores=quality, secondary_scores=efficiency)


def project_koth(
    entries: Sequence[KothEntry],
    *,
    distinct_hotkeys: bool = False,
    ceiling_band_clamp: bool = False,
) -> KothProjection | None:
    """Return the champion and participation tail for an eligible score pool.

    The fold makes the earliest ``first_seen`` the provisional champion and asks
    every later entry to clear the indifference band, so ``first_seen`` is what
    decides a tie. It is the **lineage's** first arrival at the score being
    defended, not the winning submission's upload time — the platform resolves
    that across the owner family before serving the ledger
    (``LedgerRow.crown_first_seen``). Feeding raw upload times in here instead
    reinstates the defect that motivated the distinction: a tied miner that
    resubmits re-anchors itself behind its rival and hands over the crown.
    """
    scored = [entry for entry in entries if entry.composite > 0.0]
    if not scored:
        return None

    ranked = _ranked_entries(scored)
    if _quality_primary_efficiency_active(scored):
        # Protocol 21 intentionally removes the continuous-score KOTH hold: an
        # incumbent outside the highest authoritative-quality tier cannot keep
        # the crown because it is cheaper. Efficiency chooses only inside the
        # exact top-quality tier.
        champion = ranked[0]
    else:
        ordered = sorted(scored, key=lambda entry: (entry.first_seen, entry.agent_id))
        champion = ordered[0]
        for challenger in ordered[1:]:
            if _dethrone_decision(
                challenger, champion, ceiling_band_clamp=ceiling_band_clamp
            ).dethrones:
                champion = challenger

    ranked_tail = [
        entry for entry in ranked if entry.miner_hotkey != champion.miner_hotkey
    ]
    if distinct_hotkeys:
        tail_members: list[KothEntry] = []
        seen_hotkeys = {champion.miner_hotkey}
        for entry in ranked_tail:
            if entry.miner_hotkey in seen_hotkeys:
                continue
            seen_hotkeys.add(entry.miner_hotkey)
            tail_members.append(entry)
            if len(tail_members) == KOTH_TAIL_SIZE:
                break
        tail = tuple(tail_members)
    else:
        tail = tuple(ranked_tail[:KOTH_TAIL_SIZE])
    raw_leader = ranked[0]
    decision = (
        None
        if raw_leader.agent_id == champion.agent_id
        else _dethrone_decision(
            raw_leader, champion, ceiling_band_clamp=ceiling_band_clamp
        )
    )
    return KothProjection(
        champion=champion,
        tail=tail,
        raw_leader=raw_leader,
        raw_leader_decision=decision,
    )


def _confirmations(entry: KothEntry) -> tuple[float, ...] | None:
    values = entry.confirmation_composites
    if values is None or len(values) < 2:
        return None
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
        return None
    return values


def _validated_composites(
    values: tuple[float, ...] | None, *, minimum: int
) -> tuple[float, ...] | None:
    if values is None or len(values) < minimum:
        return None
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in values):
        return None
    return values


def _efficiency_curve_version(entry: KothEntry) -> int:
    """Frozen curve that produced this entry's factor, defaulting to v3."""
    version = entry.efficiency_curve_version
    if isinstance(version, int) and is_factor_curve(version):
        return version
    return CURVE_VERSION_BOUNDED_FACTOR


def _bounded_efficiency_factor(entry: KothEntry) -> float | None:
    """Return a surfaced factor, neutralizing malformed values.

    Frozen v3 rows stay inside ``[0.85, 1.10]``. Curve v4 accepts any finite
    positive factor so an unclamped cost ratio is not silently rewritten to
    the old 1.10 cap (and then to ``first_seen``).
    """
    factor = entry.efficiency_factor
    if factor is None:
        return None
    if entry.bench_version < 9:
        return 1.0
    if (
        isinstance(factor, bool)
        or not isinstance(factor, (int, float))
        or not math.isfinite(factor)
        or factor <= 0.0
    ):
        return 1.0
    if (
        _efficiency_curve_version(entry) < CURVE_VERSION_UNBOUNDED_FACTOR
        and not 0.85 <= factor <= 1.1
    ):
        return 1.0
    return float(factor)


def _efficiency_multiplier(entry: KothEntry) -> float:
    """Return the frozen relative-efficiency multiplier.

    Curve v3 carries the multiplier itself because it may be either a bounded
    penalty or a bounded bonus. It supersedes the legacy upside-only fraction
    when present; missing data is neutral under both contracts.
    """
    factor = _bounded_efficiency_factor(entry)
    if factor is not None:
        return factor

    bonus = entry.efficiency_bonus
    if (
        isinstance(bonus, bool)
        or not isinstance(bonus, (int, float))
        or not math.isfinite(bonus)
        or not 0.0 <= bonus <= 0.1
    ):
        return 1.0
    return 1.0 + float(bonus)


def bounded_efficiency_adjusted_quality(
    quality: float,
    factor: float,
    *,
    curve_version: int = CURVE_VERSION_BOUNDED_FACTOR,
) -> float:
    """Apply a factor-curve downside or remaining-headroom upside to quality.

    The caller supplies already-validated Bench-v9 quality and factor values.
    Curve v3 uses the linear headroom form, which reaches 1.0 at ``factor = 2``
    and then has a negative stderr slope. Curve v4 uses the asymptotic form
    ``quality + (1 - quality) * (1 - 1 / factor)`` so the composite stays
    strictly below 1.0 for every finite factor and imperfect quality.
    Frozen v3 snapshots must keep the original arithmetic.
    """
    if factor <= 1.0:
        return quality * factor
    if curve_version >= CURVE_VERSION_UNBOUNDED_FACTOR:
        return quality + (1.0 - quality) * (1.0 - 1.0 / factor)
    return quality + (factor - 1.0) * (1.0 - quality)


def _efficiency_stderr_scale(entry: KothEntry) -> float:
    """Return the score transform's slope on the entry's quality scale.

    Standard error is measured before relative efficiency is applied, so its
    first-order propagation uses the derivative of the frozen transform. Curve
    v3 downside is ``quality * factor`` and therefore has slope ``factor``;
    upside is the remaining-headroom transform and has slope ``2 - factor``.
    Legacy v1/v2 rows remain multiplicative and retain their ``1 + bonus``
    scaling exactly.
    """
    factor = _bounded_efficiency_factor(entry)
    if factor is not None:
        if factor <= 1.0:
            return factor
        if _efficiency_curve_version(entry) >= CURVE_VERSION_UNBOUNDED_FACTOR:
            return 1.0 / factor
        return 2.0 - factor
    return _efficiency_multiplier(entry)


def _efficiency_adjusted_composite(entry: KothEntry, quality: float) -> float:
    """Apply curve v3 within the Bench-v9 score domain.

    A factor below one remains a multiplicative cost penalty.  Positive
    efficiency scales only the quality score's remaining headroom, preventing
    a broad plateau where distinct high-quality agents all saturate at 1.0.
    Historical v1/v2 bonus rows keep their original arithmetic so frozen
    snapshots still replay.
    """
    factor = _bounded_efficiency_factor(entry)
    if factor is not None:
        return bounded_efficiency_adjusted_quality(
            quality, factor, curve_version=_efficiency_curve_version(entry)
        )
    return quality * _efficiency_multiplier(entry)


def _effective_score_ceiling(entry: KothEntry) -> float:
    """Highest score this entry's frozen efficiency transform can attain."""
    factor = _bounded_efficiency_factor(entry)
    if factor is not None:
        return factor if factor <= 1.0 else 1.0
    return _efficiency_multiplier(entry)


def continual_composite(entry: KothEntry) -> float:
    """Return the continual aggregate before relative efficiency is applied.

    An agent starts on the robust three-validator median stored in
    ``entry.composite``. Once at least one completed cohort wave exists, the
    estimator has four or more independent observations and switches to the
    arithmetic mean of the three signed quorum scores plus one score per wave.
    Partial waves are never supplied here, so a faster cohort member cannot move
    the leaderboard before its peers complete the same seed.

    Older in-row confirmation bundles remain a compatibility fallback for
    already-issued pre-wave work. They may settle a paired KOTH comparison, but
    do not masquerade as completed continual-score waves.
    """
    quorum = _validated_composites(entry.quorum_composites, minimum=3)
    waves = _validated_composites(entry.completed_wave_composites, minimum=1)
    if quorum is not None and len(quorum) == 3 and waves is not None:
        samples = (*quorum, *waves)
        return math.fsum(samples) / len(samples)

    values = _confirmations(entry)
    if values is None:
        return entry.composite
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def effective_composite(entry: KothEntry) -> float:
    """Return the frozen efficiency projection for an entry.

    Continual evidence is aggregated first. A frozen relative-efficiency
    adjustment, when awarded and activated for the fold, then transforms that
    aggregate. Protocol-21 curve v3 uses this value only to break exact
    authoritative-quality ties; positive efficiency closes a bounded fraction
    of the remaining distance to 1.0. Legacy curves retain this value as their
    primary continuous score under their frozen arithmetic.
    Missing adjustment data remains byte-identical to the continual-only fold,
    and older benchmark token penalties remain inside their signed composites
    rather than entering this mechanism.
    """
    return _efficiency_adjusted_composite(entry, continual_composite(entry))


def _effective_composite(entry: KothEntry) -> float:
    """Backward-compatible private alias for existing callers and tests."""
    return effective_composite(entry)


def _stderr(entry: KothEntry) -> float | None:
    value = entry.composite_stderr
    if value is not None and math.isfinite(value) and value >= 0.0:
        return value
    return None


def _seed_composites(entry: KothEntry) -> dict[int, float] | None:
    composites = _confirmations(entry)
    seeds = entry.confirmation_seeds
    if composites is None or seeds is None or len(seeds) != len(composites):
        return None
    out: dict[int, float] = {}
    for seed, composite in zip(seeds, composites, strict=True):
        if seed < 0 or seed in out:
            return None
        out[seed] = composite
    return out


def _paired_statistic(
    challenger: KothEntry, champion: KothEntry
) -> tuple[float, float, float] | None:
    challenger_by_seed = _seed_composites(challenger)
    champion_by_seed = _seed_composites(champion)
    if challenger_by_seed is None or champion_by_seed is None:
        return None
    shared = sorted(challenger_by_seed.keys() & champion_by_seed.keys())
    if len(shared) < 2:
        return None
    differences = [
        _efficiency_adjusted_composite(challenger, challenger_by_seed[seed])
        - _efficiency_adjusted_composite(champion, champion_by_seed[seed])
        for seed in shared
    ]
    champion_reference = sum(
        _efficiency_adjusted_composite(champion, champion_by_seed[seed])
        for seed in shared
    ) / len(shared)
    mean_difference = sum(differences) / len(differences)
    variance = sum(
        (difference - mean_difference) ** 2 for difference in differences
    ) / (len(differences) - 1)
    return mean_difference, champion_reference, math.sqrt(variance / len(differences))


def _weight_tied(candidate: KothEntry, anchor: KothEntry) -> bool:
    """Mirror the validator's fail-closed tie grouping rule."""
    if _quality_primary_efficiency_active((candidate, anchor)):
        return continual_composite(candidate) == continual_composite(
            anchor
        ) and effective_composite(candidate) == effective_composite(anchor)
    if effective_composite(candidate) == effective_composite(anchor):
        return True
    paired = _paired_statistic(candidate, anchor)
    if paired is None:
        return False
    mean_difference, _anchor_reference, standard_error = paired
    return abs(mean_difference) <= KOTH_DETHRONE_Z * standard_error


def _dethrone_decision(
    challenger: KothEntry,
    champion: KothEntry,
    *,
    ceiling_band_clamp: bool = False,
) -> DethroneDecision:
    score_ceiling = _effective_score_ceiling(challenger)
    paired = _paired_statistic(challenger, champion)
    if paired is not None:
        lead, champion_reference, standard_error = paired
        margin_lead = KOTH_MARGIN
        paired_statistical_lead = KOTH_DETHRONE_Z * standard_error
        required = max(margin_lead, paired_statistical_lead) * _dethrone_band_scale(
            challenger, champion, champion_reference
        )
        required = _ceiling_capped_band(
            required,
            challenger,
            champion,
            champion_reference,
            active=ceiling_band_clamp,
        )
        observed_score = champion_reference + lead
        required_score = champion_reference + required
        dethrones = observed_score > required_score
        return DethroneDecision(
            challenger_lead=lead,
            required_lead=required,
            margin_lead=margin_lead,
            statistical_lead=paired_statistical_lead,
            method="paired",
            dethrones=dethrones,
            required_score=required_score,
            score_ceiling=score_ceiling,
            ceiling_deadlocked=(not dethrones and required_score >= score_ceiling),
        )

    challenger_composite = _effective_composite(challenger)
    champion_composite = _effective_composite(champion)
    lead = challenger_composite - champion_composite
    margin_lead = KOTH_MARGIN
    challenger_stderr = _stderr(challenger)
    champion_stderr = _stderr(champion)
    statistical_lead: float | None = None
    method: Literal["flat", "unpaired", "paired"] = "flat"
    if challenger_stderr is not None and champion_stderr is not None:
        challenger_stderr *= _efficiency_stderr_scale(challenger)
        champion_stderr *= _efficiency_stderr_scale(champion)
        statistical_lead = KOTH_DETHRONE_Z * math.sqrt(
            challenger_stderr**2 + champion_stderr**2
        )
        method = "unpaired"
    required = max(
        margin_lead,
        statistical_lead if statistical_lead is not None else margin_lead,
    ) * _dethrone_band_scale(challenger, champion, champion_composite)
    required = _ceiling_capped_band(
        required,
        challenger,
        champion,
        champion_composite,
        active=ceiling_band_clamp,
    )
    required_score = champion_composite + required
    dethrones = challenger_composite > required_score
    return DethroneDecision(
        challenger_lead=lead,
        required_lead=required,
        margin_lead=margin_lead,
        statistical_lead=statistical_lead,
        method=method,
        # Mirror the validator's threshold comparison. Subtracting first can
        # round an exact decimal boundary infinitesimally upward.
        dethrones=dethrones,
        required_score=required_score,
        score_ceiling=score_ceiling,
        ceiling_deadlocked=(not dethrones and required_score >= score_ceiling),
    )
