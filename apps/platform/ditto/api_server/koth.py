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

from ditto.score_order import rank_submissions

# Frozen consensus constants from ditto-subnet/ditto/validator/config.py.
KOTH_MARGIN = 0.007
# Keep these values byte-for-byte aligned with ditto-subnet's consensus fold.
# Bench v6+ shrinks the whole band; legacy/mixed comparisons remain unchanged.
KOTH_BAND_DECAY_MIN_BENCH_VERSION = 6
KOTH_BAND_DECAY_START_COMPOSITE = 0.60
KOTH_BAND_DECAY_RATE = 2.0
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


@dataclass(frozen=True)
class DethroneDecision:
    """Why the raw score leader did or did not clear the incumbent."""

    challenger_lead: float
    required_lead: float
    margin_lead: float
    statistical_lead: float | None
    method: Literal["flat", "unpaired", "paired"]
    dethrones: bool


@dataclass(frozen=True)
class KothProjection:
    champion: KothEntry
    tail: tuple[KothEntry, ...]
    raw_leader: KothEntry
    raw_leader_decision: DethroneDecision | None


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
    gap = effective_composite(cutoff) - effective_composite(candidate)
    if gap <= 0.0:
        return True
    candidate_stderr = _stderr(candidate) or 0.0
    cutoff_stderr = _stderr(cutoff) or 0.0
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
    ranked = rank_submissions(
        (
            entry
            for entry in entries
            if entry.composite > 0.0 and entry.miner_hotkey != champion.miner_hotkey
        ),
        scores=_official_scores(entries),
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


def _official_scores(entries: Iterable[KothEntry]) -> dict[UUID, float]:
    """Every entry's official composite, keyed for :func:`rank_submissions`.

    The fold ranks on :func:`effective_composite`, never on the raw ``composite``
    the entry was built from -- that is the same rule the public board's ``rank``
    and the queue's score floors follow, spelled once in
    :mod:`ditto.score_order`.
    """
    return {entry.agent_id: effective_composite(entry) for entry in entries}


def project_koth(entries: Sequence[KothEntry]) -> KothProjection | None:
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

    ordered = sorted(scored, key=lambda entry: (entry.first_seen, entry.agent_id))
    champion = ordered[0]
    for challenger in ordered[1:]:
        if _dethrone_decision(challenger, champion).dethrones:
            champion = challenger

    official = _official_scores(scored)
    tail = tuple(
        rank_submissions(
            (entry for entry in scored if entry.miner_hotkey != champion.miner_hotkey),
            scores=official,
        )[:KOTH_TAIL_SIZE]
    )
    raw_leader = rank_submissions(scored, scores=official)[0]
    decision = (
        None
        if raw_leader.agent_id == champion.agent_id
        else _dethrone_decision(raw_leader, champion)
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


def _efficiency_multiplier(entry: KothEntry) -> float:
    """Return the bounded multiplier for an awarded relative-efficiency bonus."""
    bonus = entry.efficiency_bonus
    if (
        isinstance(bonus, bool)
        or not isinstance(bonus, (int, float))
        or not math.isfinite(bonus)
        or not 0.0 <= bonus <= 0.1
    ):
        return 1.0
    return 1.0 + float(bonus)


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
    """Return the final score that drives ranking, KOTH, and emissions.

    Continual evidence is aggregated first. A frozen Bench v7+ relative token
    efficiency bonus, when awarded and activated for the fold, then multiplies
    that aggregate. Missing bonus data remains byte-identical to the continual-
    only fold, and older benchmark token penalties remain inside their signed
    composites rather than entering this mechanism.
    """
    return continual_composite(entry) * _efficiency_multiplier(entry)


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
    challenger_multiplier = _efficiency_multiplier(challenger)
    champion_multiplier = _efficiency_multiplier(champion)
    differences = [
        challenger_by_seed[seed] * challenger_multiplier
        - champion_by_seed[seed] * champion_multiplier
        for seed in shared
    ]
    champion_reference = sum(
        champion_by_seed[seed] * champion_multiplier for seed in shared
    ) / len(shared)
    mean_difference = sum(differences) / len(differences)
    variance = sum(
        (difference - mean_difference) ** 2 for difference in differences
    ) / (len(differences) - 1)
    return mean_difference, champion_reference, math.sqrt(variance / len(differences))


def _dethrone_decision(challenger: KothEntry, champion: KothEntry) -> DethroneDecision:
    paired = _paired_statistic(challenger, champion)
    if paired is not None:
        lead, champion_reference, standard_error = paired
        margin_lead = KOTH_MARGIN
        paired_statistical_lead = KOTH_DETHRONE_Z * standard_error
        required = max(margin_lead, paired_statistical_lead) * _dethrone_band_scale(
            challenger, champion, champion_reference
        )
        return DethroneDecision(
            challenger_lead=lead,
            required_lead=required,
            margin_lead=margin_lead,
            statistical_lead=paired_statistical_lead,
            method="paired",
            dethrones=(champion_reference + lead > champion_reference + required),
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
        challenger_stderr *= _efficiency_multiplier(challenger)
        champion_stderr *= _efficiency_multiplier(champion)
        statistical_lead = KOTH_DETHRONE_Z * math.sqrt(
            challenger_stderr**2 + champion_stderr**2
        )
        method = "unpaired"
    required = max(
        margin_lead,
        statistical_lead if statistical_lead is not None else margin_lead,
    ) * _dethrone_band_scale(challenger, champion, champion_composite)
    return DethroneDecision(
        challenger_lead=lead,
        required_lead=required,
        margin_lead=margin_lead,
        statistical_lead=statistical_lead,
        method=method,
        # Mirror the validator's threshold comparison. Subtracting first can
        # round an exact decimal boundary infinitesimally upward.
        dethrones=challenger_composite > champion_composite + required,
    )
