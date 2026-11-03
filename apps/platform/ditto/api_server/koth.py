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
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError

from ditto.api_models.validator import CrownGateMarker, LedgerEntry
from ditto.api_server.efficiency import (
    CURVE_VERSION_BOUNDED_FACTOR,
    CURVE_VERSION_UNBOUNDED_FACTOR,
    is_factor_curve,
)
from ditto.score_order import rank_submissions, score_order_key
from ditto_screening_protocol.dueling import (
    DuelingReport,
    dueling_composite,
    is_dueling_bench_version,
)

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
# Mirror ditto.validator.config.KOTH_STATISTICAL_BAND_CAP_MULTIPLE.
KOTH_STATISTICAL_BAND_CAP_MULTIPLE = 2.0

# One tempo = 360 blocks (~72 min at 12 s/block); mirrors the subnet worker's
# rescore cadence.  The top-5 continual shared-seed rescore lane opens rounds on
# a reign-backoff over the champion's crown (see ``top5_round_is_due``).
BLOCKS_PER_TEMPO = 360

# Continual retests gather enough shared-seed evidence to make the one-sided
# 95% uncertainty half-width no wider than the fixed KOTH margin. Eight is the
# minimum credible variance sample; 15 is the fleet-work hard stop when current
# variance would otherwise ask for an impractical number of runs.
TOP5_MIN_CONFIRMATION_SEEDS = 8
TOP5_MAX_CONFIRMATION_SEEDS = 15


@dataclass(frozen=True)
class CrownGate:
    """Mirror of the validator's protocol-34 ``CrownGate``.

    ``min_shared_seeds``: the crown moves only on the paired shared-seed
    statistic, and only once the challenger holds this many seeds in common
    with the champion. ``standing_rank``: only an entry inside the top
    ``standing_rank`` of the fold's official ranking may hold or take it.
    """

    min_shared_seeds: int
    standing_rank: int


@dataclass(frozen=True, eq=False)
class DuelingSeedRecord:
    """One bench >= 16 confirmation record: its seed, version and the signed
    ``details.dueling`` aggregates (``None`` when the record has none)."""

    seed: int
    bench_version: int
    report: DuelingReport | None


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
    # Bench >= 16 (dueling): the row's season, its signed ``details.dueling``
    # aggregates, and every confirmation record's aggregates. Mirrors
    # ``LedgerEntry.dueling_season_id`` / ``dueling`` and
    # ``ConfirmationScoreRecord.dueling``; excluded from equality and hashing
    # because the reports are value objects with list fields.
    dueling_season_id: str | None = None
    dueling: DuelingReport | None = field(default=None, compare=False)
    dueling_records: tuple[DuelingSeedRecord, ...] | None = field(
        default=None, compare=False
    )


def crown_gate_marker(gate: CrownGate | None) -> CrownGateMarker | None:
    """Wire form of a crown gate, or ``None`` when the gate is off."""
    if gate is None:
        return None
    return CrownGateMarker(
        min_dethrone_confirmations=gate.min_shared_seeds,
        champion_standing_rank=gate.standing_rank,
    )


def koth_entries_from_ledger(entries: Sequence[LedgerEntry]) -> list[KothEntry]:
    """Lift validator-wire ledger entries into the fold's public-safe shape.

    The epoch pin stores the exact ``LedgerEntry`` list validators fold, so
    the Platform's own projection of that pin -- the recorded champion, the
    incumbent handed to the next pin, the per-epoch history -- must be built
    from the same bytes rather than from a fresh database read that may
    already have moved. ``raw_rank`` is the finalized canonical-median order,
    matching :func:`_public_koth_emissions`; quorum composites come from the
    signed score proofs and completed-wave composites from the continual
    aggregate the entry carries.
    """
    by_median = sorted(entries, key=score_order_key)
    raw_ranks = {entry.agent_id: rank for rank, entry in enumerate(by_median, start=1)}
    lifted: list[KothEntry] = []
    for entry in entries:
        receipt = entry.v9_confirmation
        history = _confirmation_history(entry)
        confirmations = (
            tuple(history.values())
            if history is not None
            else entry.confirmation_composites
        )
        seeds = (
            tuple(history.keys()) if history is not None else entry.confirmation_seeds
        )
        paired_composites: tuple[float, ...] | None = None
        paired_seeds: tuple[int, ...] | None = None
        if (
            confirmations is not None
            and seeds is not None
            and len(confirmations) == len(seeds)
            and len(confirmations) >= 2
        ):
            paired_composites = tuple(confirmations)
            paired_seeds = tuple(seeds)
        lifted.append(
            KothEntry(
                miner_hotkey=entry.miner_hotkey,
                agent_id=entry.agent_id,
                composite=(
                    receipt.full_effective_micros / 1_000_000
                    if receipt is not None
                    else entry.composite
                ),
                first_seen=entry.first_seen,
                raw_rank=raw_ranks[entry.agent_id],
                bench_version=entry.bench_version or 1,
                composite_stderr=entry.composite_stderr,
                quorum_composites=(
                    ()
                    if receipt is not None
                    else tuple(proof.composite for proof in entry.score_proofs)
                ),
                completed_wave_composites=(
                    ()
                    if receipt is not None
                    else (
                        tuple(history.values())
                        if history is not None
                        and entry.continual_aggregate_method == "mean_after_quorum"
                        else (
                            ()
                            if entry.confirmation_composites is None
                            else tuple(entry.confirmation_composites)
                        )
                    )
                ),
                confirmation_composites=paired_composites,
                confirmation_seeds=paired_seeds,
                efficiency_bonus=entry.efficiency_bonus,
                efficiency_factor=entry.efficiency_factor,
                efficiency_curve_version=entry.efficiency_curve_version,
                dueling_season_id=entry.dueling_season_id,
                dueling=entry.dueling,
                # A receipt row never pairs on continual history, exactly as
                # the validator's ``_entry_seed_composites`` refuses it.
                dueling_records=(
                    None
                    if receipt is not None or not entry.confirmation_history
                    else tuple(
                        DuelingSeedRecord(
                            seed=record.seed,
                            bench_version=record.bench_version,
                            report=record.dueling,
                        )
                        for record in entry.confirmation_history
                    )
                ),
            )
        )
    return lifted


def _confirmation_history(entry: LedgerEntry) -> dict[int, float] | None:
    """Collapse modern continual evidence exactly as the validator fold does."""
    records = entry.confirmation_history
    if not records:
        return None
    grouped: dict[int, list[float]] = {}
    for record in records:
        grouped.setdefault(record.seed, []).append(record.composite)
    collapsed = {seed: _median(values) for seed, values in grouped.items() if values}
    if not collapsed:
        return None
    return dict(sorted(collapsed.items())[:TOP5_MAX_CONFIRMATION_SEEDS])


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


@dataclass(frozen=True)
class PairedStatistic:
    """Shared-seed head-to-head used by the paired dethrone rule."""

    mean_difference: float
    champion_reference: float
    standard_error: float
    differences: tuple[float, ...]


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
    paired_standard_error: float | None = None
    shared_seed_count: int | None = None
    seed_differences: tuple[float, ...] | None = None
    # Protocol 34: the comparison would clear the band but the crown gate
    # refuses it (too few shared seeds, or no paired statistic at all).
    required_shared_seeds: int | None = None
    gate_blocked: bool = False


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
    quality_primary = _quality_primary_efficiency_active((challenger, champion))
    ceiling = 1.0 if quality_primary else _effective_score_ceiling(challenger)
    headroom = ceiling - champion_score
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
    projection: KothProjection | None,
    *,
    tie_pooling: bool = False,
    statistical_band_cap: bool = False,
    rank_shares: Sequence[float] | None = None,
) -> tuple[float, ...]:
    """Return occupied rank shares, optionally pooled across evidence ties.

    ``rank_shares`` is the served protocol-31 operator curve; ``None`` keeps the
    frozen ``KOTH_RANK_SHARES``.
    """
    members = emission_set(projection)
    curve = KOTH_RANK_SHARES if rank_shares is None else tuple(rank_shares)
    shares = list(curve[: len(members)])
    if not tie_pooling:
        return tuple(shares)
    start = 0
    while start < len(members):
        anchor = members[start]
        end = start + 1
        while end < len(members) and _weight_tied(
            members[end], anchor, statistical_band_cap=statistical_band_cap
        ):
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
    statistical_band_cap: bool = False,
    rank_shares: Sequence[float] | None = None,
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
            entries,
            projection,
            ceiling_band_clamp=ceiling_band_clamp,
            statistical_band_cap=statistical_band_cap,
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
        shares=emission_shares(
            projection,
            tie_pooling=tie_pooling,
            statistical_band_cap=statistical_band_cap,
            rank_shares=rank_shares,
        ),
    )


def _score_ceiling_cohort(
    entries: Sequence[KothEntry],
    projection: KothProjection,
    *,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
) -> tuple[KothEntry, ...]:
    # Curve-v3 protocol 21 has no continuous adjusted-score ceiling: quality is
    # the primary order and efficiency only breaks an exact quality tie.
    if _quality_primary_efficiency_active(_fold_pool(entries)):
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
        challenger,
        projection.champion,
        ceiling_band_clamp=ceiling_band_clamp,
        statistical_band_cap=statistical_band_cap,
    )
    if not decision.ceiling_deadlocked:
        return ()

    anchor = ranked[0]
    cohort = [anchor]
    for entry in ranked[1:]:
        if not _weight_tied(entry, anchor, statistical_band_cap=statistical_band_cap):
            break
        cohort.append(entry)
    return tuple(cohort) if len(cohort) > 1 else ()


def champion_defense(
    entries: Sequence[KothEntry],
    projection: KothProjection | None,
    *,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
    crown_gate: CrownGate | None = None,
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
        challenger,
        projection.champion,
        ceiling_band_clamp=ceiling_band_clamp,
        statistical_band_cap=statistical_band_cap,
        crown_gate=crown_gate,
    )


@dataclass(frozen=True)
class ChallengerStanding:
    """One challenger's duel with the King, as the crown rules see it."""

    entry: KothEntry
    official_rank: int
    official_score: float
    shared_seeds: int
    within_standing: bool
    decision: DethroneDecision


@dataclass(frozen=True)
class CrownStandings:
    """The King and the challengers dueling it, in official order."""

    king_official_rank: int
    king_official_score: float
    challengers: tuple[ChallengerStanding, ...]


def crown_standings(
    entries: Sequence[KothEntry],
    projection: KothProjection | None,
    *,
    crown_gate: CrownGate,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
    limit: int = KOTH_TAIL_SIZE,
) -> CrownStandings | None:
    """Explain the crown: where the King stands and what each challenger needs.

    Challengers are the next ``limit`` distinct miners in the fold's official
    order (the board's ``rank``). Each is compared with the King through the
    same paired decision the fold runs, under ``crown_gate``, so a challenger
    shown as able to dethrone is one the gated fold would crown.
    """
    if projection is None:
        return None
    scored = _fold_pool(entries)
    ranked = _ranked_entries(scored)
    positions = {entry.agent_id: index for index, entry in enumerate(ranked, start=1)}
    quality_primary = _quality_primary_efficiency_active(scored)
    king = projection.champion
    rivals = [
        entry
        for entry in _distinct_ranked(scored)
        if entry.miner_hotkey != king.miner_hotkey
    ][: max(0, limit)]
    return CrownStandings(
        king_official_rank=positions.get(king.agent_id, len(ranked) + 1),
        king_official_score=_dethrone_composite(king, quality_primary=quality_primary),
        challengers=tuple(
            ChallengerStanding(
                entry=entry,
                official_rank=positions[entry.agent_id],
                official_score=_dethrone_composite(
                    entry, quality_primary=quality_primary
                ),
                shared_seeds=shared_seed_count(entry, king),
                within_standing=positions[entry.agent_id] <= crown_gate.standing_rank,
                decision=_dethrone_decision(
                    entry,
                    king,
                    ceiling_band_clamp=ceiling_band_clamp,
                    statistical_band_cap=statistical_band_cap,
                    crown_gate=crown_gate,
                ),
            )
            for entry in rivals
        ),
    )


def _distinct_ranked(entries: Sequence[KothEntry]) -> tuple[KothEntry, ...]:
    ranked = _ranked_entries(_fold_pool(entries))
    distinct: list[KothEntry] = []
    seen_hotkeys: set[str] = set()
    for entry in ranked:
        if entry.miner_hotkey in seen_hotkeys:
            continue
        seen_hotkeys.add(entry.miner_hotkey)
        distinct.append(entry)
    return tuple(distinct)


@dataclass(frozen=True)
class TieBandComparison:
    """The arithmetic behind one tie-tolerant cohort admission decision.

    Returned so a read-only operator view can show *why* an agent fell outside
    the cutoff instead of only that it did: the score gap it has to close, the
    band that would have forgiven it, and the resulting verdict.
    """

    gap: float
    """``score(cutoff) - score(candidate)``; negative when already ahead."""

    tolerance: float
    """``z * sqrt(se_candidate^2 + se_cutoff^2)`` on the same score scale."""

    indistinguishable: bool
    """Whether the gap is inside the band (an exact tie or better counts)."""


def tie_band_comparison(
    candidate: KothEntry, cutoff: KothEntry, *, tolerance_z: float
) -> TieBandComparison:
    """Compare ``candidate`` against the ``cutoff`` agent's tie band.

    The single expression of the band; :func:`indistinguishable_from` is the
    boolean the fold and the cohort builder call, and this is the same
    computation with its intermediate terms kept for diagnosis. Both must stay
    one implementation: an operator read that derived the band separately could
    explain an exclusion the lane did not actually make.
    """
    quality_primary = _quality_primary_efficiency_active((candidate, cutoff))
    score = continual_composite if quality_primary else effective_composite
    gap = score(cutoff) - score(candidate)
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
    return TieBandComparison(
        gap=gap,
        tolerance=tolerance,
        indistinguishable=gap <= 0.0 or gap <= tolerance,
    )


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
    return tie_band_comparison(
        candidate, cutoff, tolerance_z=tolerance_z
    ).indistinguishable


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
        for entry in _fold_pool(entries)
        if entry.miner_hotkey != champion.miner_hotkey
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


def _capped_statistical_band(margin: float, statistical: float) -> float:
    """Cap uncertainty without adding the dethrone margin to tie membership."""
    return min(statistical, KOTH_STATISTICAL_BAND_CAP_MULTIPLE * margin)


def _indifference_band(
    margin: float, statistical: float | None, *, capped: bool = True
) -> float:
    """``max(margin, statistical)``; cap when that fold branch calls for it."""
    if statistical is None:
        return margin
    if capped:
        statistical = _capped_statistical_band(margin, statistical)
    return max(margin, statistical)


def _dethrone_composite(entry: KothEntry, *, quality_primary: bool) -> float:
    """Score the hysteresis comparison uses for one entry.

    Quality-primary ledgers dethrone on authoritative quality so efficiency
    remains an exact-quality ranking tiebreak. Legacy ledgers keep the
    efficiency-adjusted composite as their continuous score.
    """
    return continual_composite(entry) if quality_primary else effective_composite(entry)


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
    statistical_band_cap: bool = False,
    incumbent_agent_id: UUID | None = None,
    crown_gate: CrownGate | None = None,
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

    Protocol-21 efficiency ranks the tail quality-first and breaks exact
    quality ties by efficiency. It does not skip this hysteresis loop. A
    later 0.001 official lead is the raw leader, not the champion.

    A protocol-34 ``crown_gate`` mirrors the validator's ``_champion``: an
    opener outside the top ``standing_rank`` official places hands the crown
    to the highest-ranked standing entry holding ``min_shared_seeds`` shared
    seeds against it (else the rank-1 entry), and only standing challengers
    are walked, each needing a paired win on ``min_shared_seeds`` seeds.
    """
    scored = _fold_pool(entries)
    if not scored:
        return None

    ranked = _ranked_entries(scored)
    ordered = sorted(scored, key=lambda entry: (entry.first_seen, entry.agent_id))
    # Crown incumbency (``crown_mode: incumbent``): the previous epoch's
    # champion, resolved into this pool, opens the walk instead of the earliest
    # lineage, so a senior claimant inside the band no longer retakes the crown
    # on every read. Every other entry still has to clear the band over the
    # running champion, in the same first-seen order. An incumbent that is not
    # in the pool (bootstrap, lineage gone) falls back to the classic walk.
    incumbent = next(
        (entry for entry in scored if entry.agent_id == incumbent_agent_id), None
    )
    if incumbent is not None:
        champion = incumbent
        challengers = [
            entry for entry in ordered if entry.agent_id != incumbent.agent_id
        ]
    else:
        champion = ordered[0]
        challengers = ordered[1:]
    if crown_gate is not None:
        standing = ranked[: max(1, crown_gate.standing_rank)]
        standing_ids = {entry.agent_id for entry in standing}
        if champion.agent_id not in standing_ids:
            outgoing = champion
            champion = next(
                (
                    entry
                    for entry in standing
                    if shared_seed_count(entry, outgoing) >= crown_gate.min_shared_seeds
                ),
                standing[0],
            )
        challengers = [
            entry
            for entry in ordered
            if entry.agent_id in standing_ids and entry.agent_id != champion.agent_id
        ]
    for challenger in challengers:
        if _dethrone_decision(
            challenger,
            champion,
            ceiling_band_clamp=ceiling_band_clamp,
            statistical_band_cap=statistical_band_cap,
            crown_gate=crown_gate,
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
        tail = tuple(_dueling_adjacent_order(tail_members))
    else:
        tail = tuple(_dueling_adjacent_order(ranked_tail[:KOTH_TAIL_SIZE]))
    raw_leader = ranked[0]
    decision = (
        None
        if raw_leader.agent_id == champion.agent_id
        else _dethrone_decision(
            raw_leader,
            champion,
            ceiling_band_clamp=ceiling_band_clamp,
            statistical_band_cap=statistical_band_cap,
            crown_gate=crown_gate,
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


# --- Dueling bench (bench_version >= 16) -------------------------------------
#
# Mirrors ``ditto/validator/weights.py`` (``research/dittobench-datagen/docs/
# dueling-bench.md`` sections 3 and 7.7) byte for byte, all inert below v16:
# two v16 rows compare only inside one ``(bench_version, season_id)``, the
# paired statistic recomputes every shared seed's composite for both agents
# with ``{owner(A), owner(B)}`` masked, the paid tail is ordered by the same
# pair-masked comparison between neighbours, and a v16 row without its season
# or aggregates never ranks or pays. ``ditto/tests/fixtures/
# crown_gate_cases.json`` folds both implementations over the same cases.


def _is_dueling_entry(entry: KothEntry) -> bool:
    return is_dueling_bench_version(entry.bench_version)


def _entry_season(entry: KothEntry) -> str | None:
    """The row's season, cross-checked against its signed report's season."""
    report = entry.dueling
    if report is None:
        return None
    if entry.dueling_season_id is None:
        return report.season_id
    return (
        entry.dueling_season_id if entry.dueling_season_id == report.season_id else None
    )


def dueling_fold_ready(entry: KothEntry) -> bool:
    """Pre-16 rows always; a v16 row only with its season and aggregates."""
    return not _is_dueling_entry(entry) or _entry_season(entry) is not None


def _fold_pool(entries: Iterable[KothEntry]) -> list[KothEntry]:
    """Positive rows the fold may rank: the validator's ``filter_eligible``
    plus its positive-composite filter."""
    return [
        entry
        for entry in entries
        if entry.composite > 0.0 and dueling_fold_ready(entry)
    ]


def _dueling_incomparable(a: KothEntry, b: KothEntry) -> bool:
    """Both v16+ without one shared ``(bench_version, season_id)``."""
    if not (_is_dueling_entry(a) and _is_dueling_entry(b)):
        return False
    season_a = _entry_season(a)
    return (
        season_a is None
        or season_a != _entry_season(b)
        or a.bench_version != b.bench_version
    )


def _dueling_pair_mask(a: KothEntry, b: KothEntry) -> frozenset[str] | None:
    """``{owner(A), owner(B)}`` for two comparable v16 rows, else ``None``."""
    if not (_is_dueling_entry(a) and _is_dueling_entry(b)):
        return None
    if _dueling_incomparable(a, b):
        return None
    if a.dueling is None or b.dueling is None:
        return None
    return frozenset({a.dueling.owner_key, b.dueling.owner_key})


def ledger_row_dueling_fields(row: object) -> dict[str, Any]:
    """``KothEntry`` dueling keyword arguments from a Platform ledger row.

    Database-sourced projections carry the representative score's season and
    signed ``details.dueling`` so a v16 row stays in the pool (the fold drops
    one without them). A stored report that no longer validates is withheld,
    which the fold reads as no dueling evidence. Per-seed records come only
    from the served ledger (:func:`koth_entries_from_ledger`).
    """
    season = getattr(row, "dueling_season_id", None)
    raw = getattr(row, "dueling", None)
    report: DuelingReport | None = None
    if isinstance(raw, DuelingReport):
        report = raw
    elif isinstance(raw, dict):
        try:
            report = DuelingReport.model_validate(raw)
        except ValidationError:
            report = None
    return {
        "dueling_season_id": None if season is None else str(season),
        "dueling": report,
    }


def _record_masked_composite(
    entry: KothEntry, record: DuelingSeedRecord, mask: frozenset[str]
) -> float | None:
    """One record's composite with ``mask``'s columns removed, when the record
    is evidence for this row (same season, owner and bench version)."""
    report = record.report
    if report is None or entry.dueling is None:
        return None
    if (
        report.season_id != _entry_season(entry)
        or report.owner_key != entry.dueling.owner_key
        or record.bench_version != entry.bench_version
    ):
        return None
    value = dueling_composite(
        report.datagen_raw, report.datagen_gate, report.columns, mask
    )
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return value


def _dueling_seed_composites(
    entry: KothEntry, mask: frozenset[str]
) -> dict[int, float] | None:
    """Per-seed pair-masked medians over the bounded confirmation window."""
    records = entry.dueling_records
    if not records:
        return None
    window = set(
        sorted({record.seed for record in records})[:TOP5_MAX_CONFIRMATION_SEEDS]
    )
    grouped: dict[int, list[float]] = {}
    for record in records:
        if record.seed not in window:
            continue
        value = _record_masked_composite(entry, record, mask)
        if value is not None:
            grouped.setdefault(record.seed, []).append(value)
    if not grouped:
        return None
    return {seed: _median(values) for seed, values in sorted(grouped.items())}


def _paired_seed_maps(
    a: KothEntry, b: KothEntry
) -> tuple[dict[int, float], dict[int, float]] | None:
    """The per-seed maps a pairing compares (pair-masked for two v16 rows)."""
    if _is_dueling_entry(a) and _is_dueling_entry(b):
        mask = _dueling_pair_mask(a, b)
        if mask is None:
            return None
        a_map = _dueling_seed_composites(a, mask)
        b_map = _dueling_seed_composites(b, mask)
    else:
        a_map = _seed_composites(a)
        b_map = _seed_composites(b)
    if a_map is None or b_map is None:
        return None
    return a_map, b_map


def _dueling_adjacent_order(ranked: Sequence[KothEntry]) -> list[KothEntry]:
    """Order paid tail slots by the pair-masked comparison between neighbours.

    Mirrors the validator's ``_dueling_adjacent_order``: membership never
    moves, adjacent same-season v16 rows swap on a strictly positive paired
    lead, and the passes are bounded by the list length.
    """
    ordered = list(ranked)
    if not any(_is_dueling_entry(entry) for entry in ordered):
        return ordered
    for _ in range(len(ordered)):
        swapped = False
        for index in range(len(ordered) - 1):
            upper, lower = ordered[index], ordered[index + 1]
            if _dueling_pair_mask(lower, upper) is None:
                continue
            paired = _paired_statistic(lower, upper)
            if paired is not None and paired.mean_difference > 0.0:
                ordered[index], ordered[index + 1] = lower, upper
                swapped = True
        if not swapped:
            break
    return ordered


def shared_seed_count(a: KothEntry, b: KothEntry) -> int:
    """Confirmation seeds both entries carry a validated composite for.

    Mirrors the validator's ``_shared_seed_count``: the depth the protocol-34
    crown gate counts, and the depth the public board reports per challenger.
    Two v16 rows count pair-masked seeds inside one season.
    """
    maps = _paired_seed_maps(a, b)
    if maps is None:
        return 0
    a_map, b_map = maps
    return len(a_map.keys() & b_map.keys())


def _paired_statistic(
    challenger: KothEntry, champion: KothEntry
) -> PairedStatistic | None:
    maps = _paired_seed_maps(challenger, champion)
    if maps is None:
        return None
    challenger_by_seed, champion_by_seed = maps
    shared = sorted(challenger_by_seed.keys() & champion_by_seed.keys())
    if len(shared) < 2:
        return None
    quality_primary = _quality_primary_efficiency_active((challenger, champion))

    def _seed_score(entry: KothEntry, quality: float) -> float:
        if quality_primary:
            return quality
        return _efficiency_adjusted_composite(entry, quality)

    differences = [
        _seed_score(challenger, challenger_by_seed[seed])
        - _seed_score(champion, champion_by_seed[seed])
        for seed in shared
    ]
    champion_reference = sum(
        _seed_score(champion, champion_by_seed[seed]) for seed in shared
    ) / len(shared)
    mean_difference = sum(differences) / len(differences)
    variance = sum(
        (difference - mean_difference) ** 2 for difference in differences
    ) / (len(differences) - 1)
    return PairedStatistic(
        mean_difference=mean_difference,
        champion_reference=champion_reference,
        standard_error=math.sqrt(variance / len(differences)),
        differences=tuple(differences),
    )


def _weight_tied(
    candidate: KothEntry, anchor: KothEntry, *, statistical_band_cap: bool = False
) -> bool:
    """Mirror the validator's fail-closed tie grouping rule."""
    if _dueling_incomparable(candidate, anchor):
        return False
    if _quality_primary_efficiency_active((candidate, anchor)):
        return continual_composite(candidate) == continual_composite(
            anchor
        ) and effective_composite(candidate) == effective_composite(anchor)
    if effective_composite(candidate) == effective_composite(anchor):
        return True
    paired = _paired_statistic(candidate, anchor)
    if paired is None:
        return False
    if not statistical_band_cap:
        return abs(paired.mean_difference) <= KOTH_DETHRONE_Z * paired.standard_error
    return abs(paired.mean_difference) <= _capped_statistical_band(
        KOTH_MARGIN,
        KOTH_DETHRONE_Z * paired.standard_error,
    )


def _dethrone_decision(
    challenger: KothEntry,
    champion: KothEntry,
    *,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
    crown_gate: CrownGate | None = None,
) -> DethroneDecision:
    """Compare one challenger with the champion exactly as the fold does.

    ``ceiling_deadlocked`` always reflects the ungated comparison, as the
    validator's score-ceiling cohort does. A protocol-34 ``crown_gate`` only
    vetoes ``dethrones``: without a paired statistic on at least
    ``min_shared_seeds`` shared seeds the crown cannot move, whatever the lead.
    """
    decision = _ungated_dethrone_decision(
        challenger,
        champion,
        ceiling_band_clamp=ceiling_band_clamp,
        statistical_band_cap=statistical_band_cap,
    )
    if _dueling_incomparable(challenger, champion):
        # Two v16 rows from different seasons never dethrone each other (the
        # validator's ``_beats``); the ceiling read stays the ungated one.
        decision = replace(decision, dethrones=False)
    if crown_gate is None:
        return decision
    qualified = (
        decision.method == "paired"
        and shared_seed_count(challenger, champion) >= crown_gate.min_shared_seeds
    )
    return replace(
        decision,
        dethrones=decision.dethrones and qualified,
        required_shared_seeds=crown_gate.min_shared_seeds,
        gate_blocked=decision.dethrones and not qualified,
    )


def _ungated_dethrone_decision(
    challenger: KothEntry,
    champion: KothEntry,
    *,
    ceiling_band_clamp: bool = False,
    statistical_band_cap: bool = False,
) -> DethroneDecision:
    quality_primary = _quality_primary_efficiency_active((challenger, champion))
    score_ceiling = 1.0 if quality_primary else _effective_score_ceiling(challenger)
    paired = _paired_statistic(challenger, champion)
    if paired is not None:
        margin_lead = KOTH_MARGIN
        paired_statistical_lead = _indifference_band(
            margin_lead,
            KOTH_DETHRONE_Z * paired.standard_error,
        )
        required = paired_statistical_lead * _dethrone_band_scale(
            challenger, champion, paired.champion_reference
        )
        required = _ceiling_capped_band(
            required,
            challenger,
            champion,
            paired.champion_reference,
            active=ceiling_band_clamp,
        )
        observed_score = paired.champion_reference + paired.mean_difference
        required_score = paired.champion_reference + required
        dethrones = observed_score > required_score
        return DethroneDecision(
            challenger_lead=paired.mean_difference,
            required_lead=required,
            margin_lead=margin_lead,
            statistical_lead=paired_statistical_lead,
            method="paired",
            dethrones=dethrones,
            required_score=required_score,
            score_ceiling=score_ceiling,
            ceiling_deadlocked=(not dethrones and required_score >= score_ceiling),
            paired_standard_error=paired.standard_error,
            shared_seed_count=len(paired.differences),
            seed_differences=paired.differences,
        )

    challenger_composite = _dethrone_composite(
        challenger, quality_primary=quality_primary
    )
    champion_composite = _dethrone_composite(champion, quality_primary=quality_primary)
    lead = challenger_composite - champion_composite
    margin_lead = KOTH_MARGIN
    challenger_stderr = _stderr(challenger)
    champion_stderr = _stderr(champion)
    statistical_lead: float | None = None
    method: Literal["flat", "unpaired", "paired"] = "flat"
    if challenger_stderr is not None and champion_stderr is not None:
        if not quality_primary:
            challenger_stderr *= _efficiency_stderr_scale(challenger)
            champion_stderr *= _efficiency_stderr_scale(champion)
        statistical_lead = KOTH_DETHRONE_Z * math.sqrt(
            challenger_stderr**2 + champion_stderr**2
        )
        method = "unpaired"
    required = _indifference_band(
        margin_lead, statistical_lead, capped=statistical_band_cap
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
