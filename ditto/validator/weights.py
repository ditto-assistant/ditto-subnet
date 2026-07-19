"""KOTH + ATH-gate weight function: map the score ledger to a weight vector.

The incentive mechanism (``docs/VALIDATOR.md``): the reigning
all-time-high holder is the **champion** and takes ~all emissions; a challenger
only dethrones it by beating its score by a **relative margin** (default 2%);
ties and sub-margin gains keep the incumbent, so **first-to-submit wins** and a
downloaded copy — which at best ties — never earns. A small **participation
tail** (default 10% over the next few miners) keeps the subnet populated without
reopening the copy hole.

This is a *deterministic* fold over the platform's best-score-per-miner ledger
(``GET /scoring/scores``): every validator runs this identical function on the
identical pool, so Yuma consensus converges and clips any deviator. It must stay
pure — no I/O, no clock, no rounding (compare the raw reported doubles) — or two
validators could disagree. It lives here so the mechanism is a one-function
change, mirroring the platform's ledger read.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.validator import LedgerEntry

# Benchmark scores are only comparable within one bench_version: a version bump
# changes what the composite means, so folding a v2 champion against a v3
# challenger would be nonsense. Entries whose bench_version
# the platform does not (yet) surface are treated as this baseline — so on a
# ledger with no version info the fold is unchanged (all one version).
DEFAULT_BENCH_VERSION = 1

# A run must administer the *full* benchmark to earn emissions. The dittobench-api
# run-size profiles are small = 12 cases (6 tool + 6 memory), medium ~= 42, full =
# 60 tool + 50 memory + 4 isolation ~= 114 (dittobench-api internal/gen/gen.go). A
# smaller profile omits the hard anti-overfit memory categories entirely and its
# tiny memory suite is trivially aced, so its composite is neither comparable nor
# discriminative — folding it into weights would pay emissions for a smoke run. So
# entries below this floor are dropped from the fold (they may still appear on the
# leaderboard, marked provisional). Keep in sync with the platform's
# MIN_ELIGIBLE_CASES (ditto-platform ditto/db/queries/scores.py).
MIN_ELIGIBLE_CASES = 100


def apply_miner_emission_cap(
    weights: dict[str, float], *, miner_share: float, burn_hotkey: str
) -> dict[str, float]:
    """Reserve ``1 - miner_share`` for Subtensor's subnet-owner burn path.

    Pylon normalizes every submitted vector, so merely scaling miner weights to
    sum to ``miner_share`` would still pay miners 100%. The final vector must
    include the subnet owner's registered hotkey: Subtensor withholds and burns
    miner incentive routed to an owner-associated hotkey. The eligible miner
    vector is normalized before receiving its fixed share so a lone champion
    receives exactly ``miner_share`` rather than its raw KOTH share.

    With no positive eligible miner weights, route the whole vector to burn.
    The burn hotkey is excluded from the miner pool defensively.
    """
    if not 0.0 <= miner_share <= 1.0:
        raise ValueError(f"miner_share must be in [0, 1], got {miner_share}")
    if not burn_hotkey:
        raise ValueError("burn_hotkey must be non-empty")

    miners = {
        hotkey: weight
        for hotkey, weight in weights.items()
        if hotkey != burn_hotkey and weight > 0.0
    }
    total = sum(miners.values())
    if total <= 0.0:
        return {burn_hotkey: 1.0}

    capped = {
        hotkey: (weight / total) * miner_share for hotkey, weight in miners.items()
    }
    burn_share = 1.0 - miner_share
    if burn_share > 0.0:
        capped[burn_hotkey] = burn_share
    return capped


def _entry_version(entry: LedgerEntry) -> int:
    """The entry's bench_version, or DEFAULT_BENCH_VERSION when the platform
    ledger does not carry one. Read via getattr so the wire model can stay
    untouched; the platform surfacing bench_version on the ledger is optional,
    and until then this is a safe no-op)."""
    v = getattr(entry, "bench_version", None)
    return v if isinstance(v, int) and v > 0 else DEFAULT_BENCH_VERSION


def max_bench_version(entries: Sequence[LedgerEntry]) -> int:
    """The newest bench_version present in the ledger."""
    return max((_entry_version(e) for e in entries), default=DEFAULT_BENCH_VERSION)


def _entry_eligible(entry: LedgerEntry) -> bool:
    """Whether the entry's run administered the full benchmark and may earn
    emissions (``n >= MIN_ELIGIBLE_CASES``). Read via getattr so the wire model
    can stay untouched; an entry that carries **no** case count at all is treated
    as eligible (fail open — mirrors :func:`_entry_version`'s handling of a missing
    bench_version, so a ledger that does not surface ``n`` leaves the fold
    unchanged). The real ``LedgerEntry`` always carries ``n`` (a required wire
    field), so in production this drops exactly the runs that report ``n`` below
    the floor — the smoke/practice profiles."""
    n = getattr(entry, "n", None)
    return not isinstance(n, int) or n >= MIN_ELIGIBLE_CASES


def filter_eligible(entries: Sequence[LedgerEntry]) -> list[LedgerEntry]:
    """Keep only full-benchmark entries — the only runs that may rank or earn
    emissions. A **deterministic, consensus-safe** filter (keys off the per-entry
    case count in the shared ledger), mirroring :func:`filter_to_latest_version`,
    so every validator folds the same subset."""
    return [e for e in entries if _entry_eligible(e)]


def filter_to_latest_version(entries: Sequence[LedgerEntry]) -> list[LedgerEntry]:
    """Keep only entries at the max bench_version present — the only scores the
    weight fold may compare (§9 step 3). This is a **deterministic, consensus-safe**
    filter: it keys off the versions present in the shared ledger, not off any
    single validator's scorer version, so every validator folds the same subset."""
    latest = max_bench_version(entries)
    return [e for e in entries if _entry_version(e) == latest]


def compute_weights(
    entries: Sequence[LedgerEntry],
    *,
    margin: float,
    tail_size: int,
    champion_share: float,
    dethrone_z: float = 0.0,
) -> dict[str, float]:
    """Return ``{miner_hotkey: weight}`` for the KOTH+ATH mechanism.

    ``entries`` is the ledger: one best-scoring agent per miner. The champion is
    found by folding entries in **first-seen order** (``first_seen`` then
    ``agent_id`` to break timestamp ties) and dethroning the running champion
    only when a later entry's composite clears the **indifference band**
    (:func:`_beats`): the larger of the flat relative ``margin`` and, when the
    ledger surfaces per-entry ``composite_stderr`` and ``dethrone_z > 0``, the
    statistical band ``dethrone_z * sqrt(se_c² + se_champ²)`` — so a challenger
    inside the measurement noise cannot flip the crown. With no stderr the
    band is exactly ``margin`` relative, identical to the pre-band rule. The
    champion gets ``champion_share``; the next ``tail_size`` miners by composite
    split ``1 - champion_share`` equally.

    Only entries at the **max bench_version present** are folded (§9 step 3):
    scores under an older benchmark version are incomparable and dropped until a
    re-score lifts them to the current version. Entries below the full-benchmark
    case floor (:func:`filter_eligible`) are likewise dropped: a smoke/practice
    run omits the hard memory categories and is trivially aced, so it must never
    become champion or take a tail slot.

    Non-positive composites are dropped (a zero-scoring miner earns nothing).
    Returns an empty dict when no miner scored above zero — the caller then skips
    ``put_weights`` rather than zeroing the chain. Pylon/Subtensor normalizes the
    returned vector, so only the ratios matter; when there is no tail the champion
    is the whole vector.
    """
    eligible = filter_eligible(filter_to_latest_version(entries))
    scored = [e for e in eligible if e.composite > 0.0]
    if not scored:
        return {}

    # Champion: fold in creation order; a later entry must beat the running
    # champion by the relative margin to take the crown. Order-independent of
    # when each agent happened to be scored — only creation order matters.
    champion = _champion(scored, margin, dethrone_z)
    weights: dict[str, float] = {champion.miner_hotkey: champion_share}

    # Tail: the next distinct miners by composite (highest first), excluding the
    # champion, split the remaining share equally.
    tail_pool = 1.0 - champion_share
    if tail_size > 0 and tail_pool > 0.0:
        runners_up = _tail(scored, champion, tail_size)
        if runners_up:
            per_miner = tail_pool / len(runners_up)
            for e in runners_up:
                weights[e.miner_hotkey] = per_miner

    return weights


def _entry_confirmations(entry: LedgerEntry) -> list[float] | None:
    """The entry's per-seed confirmation composites, or None when the ledger
    does not carry them (prod hardening P4). Read via getattr so the wire model
    can stay untouched — until the platform surfaces ``confirmation_composites``
    this is inert and the fold uses the raw composite, byte-identical to today.
    Requires at least two finite values in [0, 1]; anything else is treated as
    absent (a consensus-safe guard: one validator must never fold a different
    effective composite than another off a malformed list)."""
    v = getattr(entry, "confirmation_composites", None)
    if not isinstance(v, (list, tuple)) or len(v) < 2:
        return None
    out: list[float] = []
    for x in v:
        if (
            not isinstance(x, (int, float))
            or not math.isfinite(x)
            or not 0.0 <= x <= 1.0
        ):
            return None
        out.append(float(x))
    return out


def _effective_composite(entry: LedgerEntry) -> float:
    """The composite the dethrone comparison uses: the MEDIAN of the entry's
    per-seed confirmation composites when the ledger surfaces them, else the raw
    single-run composite. Multi-seed medians make a crown flip require a lead
    that survives seed-to-seed variance, not one lucky draw (P4). Pure and
    deterministic: an explicit sorted-middle median, no library rounding, so
    every validator computes identical bytes."""
    vals = _entry_confirmations(entry)
    if vals is None:
        return entry.composite
    s = sorted(vals)
    mid = len(s) // 2
    if len(s) % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _entry_stderr(entry: LedgerEntry) -> float | None:
    """The entry's composite standard error, or None when the platform ledger
    does not carry one. Read via getattr so the wire model can stay untouched
    (the platform surfacing ``composite_stderr`` is optional; until then the
    statistical band is inert and the fold uses the flat relative margin,
    byte-identical to today). Non-finite or
    negative values are treated as absent (a consensus-safe guard)."""
    v = getattr(entry, "composite_stderr", None)
    if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0.0:
        return float(v)
    return None


def _entry_seed_composites(entry: LedgerEntry) -> dict[int, float] | None:
    """The entry's per-seed confirmation composites keyed by their CRN seed, or
    None when the ledger does not carry an aligned ``confirmation_seeds`` +
    ``confirmation_composites`` pair (prod hardening P5). Read via getattr so the
    wire model can stay untouched — inert until the platform surfaces
    ``confirmation_seeds``, byte-identical to today. Requires equal-length lists
    of at least two validated composites (:func:`_entry_confirmations`) and
    non-negative int seeds with no duplicate; anything else is treated as absent
    (a consensus-safe guard so two validators never pair off a differently-parsed
    map)."""
    comps = _entry_confirmations(entry)
    if comps is None:
        return None
    seeds = getattr(entry, "confirmation_seeds", None)
    if not isinstance(seeds, (list, tuple)) or len(seeds) != len(comps):
        return None
    out: dict[int, float] = {}
    for s, c in zip(seeds, comps, strict=True):
        if isinstance(s, bool) or not isinstance(s, int) or s < 0 or s in out:
            return None
        out[s] = c
    return out


def _paired_dethrone(
    challenger: LedgerEntry, champion: LedgerEntry, dethrone_z: float
) -> tuple[float, float, float] | None:
    """Paired dethrone statistic over the two entries' SHARED CRN seeds (P5), or
    None when they do not share at least two confirmation seeds (or ``dethrone_z
    <= 0``). Returns ``(mean_diff, champ_ref, se_diff)``:

        mean_diff = mean over shared seeds of (challenger − champion)
        champ_ref = mean of the champion's composite over those seeds
        se_diff   = SEM of the per-seed differences

    Because the confirmation sweep scores both agents on the SAME common seeds,
    the per-seed composites are paired: differencing them on each shared seed
    cancels that seed's dataset difficulty, so ``se_diff`` is strictly smaller
    than the independent-sum band ``sqrt(se_c² + se_champ²)`` whenever the two
    agents' scores are positively correlated across seeds (the norm under CRN) —
    a tighter band at the SAME confidence, without more seeds. The variance term
    also absorbs a lucky single-seed outlier (it inflates both mean_diff and
    se_diff), so the paired mean is safe here where P4 needed the median. Pure
    and deterministic: sorted shared seeds, explicit float arithmetic."""
    if dethrone_z <= 0.0:
        return None
    chall_map = _entry_seed_composites(challenger)
    champ_map = _entry_seed_composites(champion)
    if chall_map is None or champ_map is None:
        return None
    common = sorted(set(chall_map) & set(champ_map))
    if len(common) < 2:
        return None
    diffs = [chall_map[s] - champ_map[s] for s in common]
    n = len(diffs)
    mean_diff = sum(diffs) / n
    champ_ref = sum(champ_map[s] for s in common) / n
    var = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    se_diff = math.sqrt(var / n)
    return mean_diff, champ_ref, se_diff


def _beats(
    challenger: LedgerEntry, champion: LedgerEntry, margin: float, dethrone_z: float
) -> bool:
    """Whether ``challenger`` dethrones ``champion``. The lead must exceed the
    **indifference band** = max(flat relative margin, statistical band).

    When both entries carry aligned per-seed confirmation composites over at
    least two SHARED CRN seeds (P5) and ``dethrone_z > 0``, the statistical term
    is a **paired** z-test (:func:`_paired_dethrone`): the lead is the mean
    per-seed difference and the band is ``dethrone_z * se_diff``, where se_diff is
    the SEM of the paired differences. Pairing cancels shared dataset difficulty,
    so the band is tighter than the unpaired form at the same confidence.

    Otherwise the **unpaired** rule applies (byte-identical to before):

        band = max( margin * champion.composite,
                    dethrone_z * sqrt(se_challenger² + se_champion²) )

    a two-sample z-test that engages only when BOTH entries carry a
    ``composite_stderr`` and ``dethrone_z > 0``; with no stderr (or z=0) the band
    is exactly the flat relative margin. Both sides use
    :func:`_effective_composite` (the MEDIAN over confirmation seeds when present,
    else the raw composite). Pure and deterministic (consensus-safe)."""
    paired = _paired_dethrone(challenger, champion, dethrone_z)
    if paired is not None:
        mean_diff, champ_ref, se_diff = paired
        band = max(champ_ref * margin, dethrone_z * se_diff)
        return mean_diff > band

    chall = _effective_composite(challenger)
    champ = _effective_composite(champion)
    band = champ * margin
    if dethrone_z > 0.0:
        se_c = _entry_stderr(challenger)
        se_champ = _entry_stderr(champion)
        if se_c is not None and se_champ is not None:
            stat_band = dethrone_z * math.sqrt(se_c * se_c + se_champ * se_champ)
            if stat_band > band:
                band = stat_band
    return chall - champ > band


def _champion(
    entries: Sequence[LedgerEntry], margin: float, dethrone_z: float = 0.0
) -> LedgerEntry:
    """The KOTH champion of a positive-composite entry set: fold in first-seen
    order, dethroning only when a later entry clears the indifference band
    (:func:`_beats`)."""
    ordered = sorted(entries, key=lambda e: (e.first_seen, e.agent_id))
    champ = ordered[0]
    for e in ordered[1:]:
        if _beats(e, champ, margin, dethrone_z):
            champ = e
    return champ


def _tail(
    entries: Sequence[LedgerEntry], champion: LedgerEntry, tail_size: int
) -> list[LedgerEntry]:
    """The next ``tail_size`` distinct miners by composite, excluding the champion."""
    return sorted(
        (e for e in entries if e.miner_hotkey != champion.miner_hotkey),
        key=lambda e: (-e.composite, e.first_seen, e.agent_id),
    )[:tail_size]


def agents_needing_rescore(
    entries: Sequence[LedgerEntry],
    *,
    current_version: int,
    margin: float,
    tail_size: int,
    dethrone_z: float = 0.0,
) -> list[LedgerEntry]:
    """The champion + participation-tail entries scored under an **older**
    bench_version than ``current_version`` — they must be re-evaluated before the
    fold can compare them at the new version.

    Selection uses the same champion/tail logic as :func:`compute_weights`, but
    over the FULL positive-composite set (not the version filter) — on a fresh
    bump every ledger entry may be stale, and it is exactly the reigning
    champion + tail whose scores must be refreshed first so weight-setting does
    not strand on an empty current-version subset. Returns the ledger entries
    (with ``agent_id``) so the caller can request their re-evaluation.
    """
    scored = [e for e in entries if e.composite > 0.0]
    if not scored:
        return []
    champion = _champion(scored, margin, dethrone_z)
    rewarded = [champion, *_tail(scored, champion, tail_size)]
    return [e for e in rewarded if _entry_version(e) < current_version]


def _unpaired_band(
    challenger: LedgerEntry, champion: LedgerEntry, margin: float, dethrone_z: float
) -> float:
    """The unpaired indifference band :func:`_beats` applies to this pair:
    ``max(margin * eff(champion), dethrone_z * sqrt(se_c² + se_champ²))``, the
    statistical term engaging only when both entries carry a stderr."""
    band = _effective_composite(champion) * margin
    if dethrone_z > 0.0:
        se_c = _entry_stderr(challenger)
        se_champ = _entry_stderr(champion)
        if se_c is not None and se_champ is not None:
            stat_band = dethrone_z * math.sqrt(se_c * se_c + se_champ * se_champ)
            if stat_band > band:
                band = stat_band
    return band


def _entry_has_seeds(entry: LedgerEntry, seeds: Sequence[int]) -> bool:
    """Whether ``entry`` already carries confirmation composites for every seed
    in ``seeds`` — i.e. it has been confirmed on that exact seed set and does
    not need re-scoring. Used to keep the champion from being re-scored on its
    own anchored seeds once it already holds them."""
    have = _entry_seed_composites(entry)
    if have is None:
        return False
    return set(seeds).issubset(have.keys())


def _shares_confirmation_seeds(a: LedgerEntry, b: LedgerEntry) -> bool:
    """Whether the paired dethrone statistic can already decide this pair:
    both entries carry per-seed confirmation composites over at least two
    shared CRN seeds (the :func:`_paired_dethrone` precondition)."""
    a_map = _entry_seed_composites(a)
    b_map = _entry_seed_composites(b)
    if a_map is None or b_map is None:
        return False
    return len(set(a_map) & set(b_map)) >= 2


def contested_confirmation_set(
    entries: Sequence[LedgerEntry],
    *,
    current_version: int,
    margin: float,
    dethrone_z: float = 0.0,
) -> list[LedgerEntry]:
    """The champion plus the current-version challengers whose crown decision
    sits INSIDE the unpaired indifference band (the seed-luck zone) and cannot
    yet be decided by the paired statistic. Empty when no contest needs
    confirmation.

    A dethrone resolved inside the band on unpaired data can ride dataset
    difficulty: the champion's confirmation composites are a frozen draw (they
    only refresh on a bench_version bump) while a new challenger holds one
    commit-reveal seed, so neither side's luck cancels. Re-scoring both on a
    common CRN seed set gives :func:`_paired_dethrone` the shared-seed data
    that cancels difficulty, and the crown then moves (or holds) on the paired
    statistic instead of the draw.

    Selection, all over the public ledger so every validator derives the same
    set (consensus-safe):

    - champion: the fold's champion (:func:`_champion` over the full
      positive-composite set);
    - challengers: every other current-version entry whose effective composite
      sits within the unpaired band of the champion on either side
      (``|eff(challenger) − eff(champion)| <= band``) AND that does not yet
      share >= 2 confirmation seeds with the champion. Clear wins, clear
      losses, and already-settled pairs are excluded. Stale entries are
      excluded too, since refreshing them is
      :func:`agents_needing_rescore`'s job and runs first.

    Returns the champion followed by the unsettled in-band challengers
    (deterministic first-seen order), or ``[]`` when no challenger needs
    confirmation. The caller anchors the CRN seed set to the CHAMPION's agent
    id alone, not the contested set, so a newly appearing challenger is scored
    once on the champion's unchanged seeds while already-settled challengers
    keep sharing them and are never re-scored: cost is O(1) per new
    challenger, not O(cohort) per sweep.
    """
    scored = [e for e in entries if e.composite > 0.0]
    if len(scored) < 2:
        return []
    champion = _champion(scored, margin, dethrone_z)
    if _entry_version(champion) != current_version:
        return []
    contested = [
        e
        for e in sorted(scored, key=lambda e: (e.first_seen, e.agent_id))
        if e.agent_id != champion.agent_id
        and _entry_version(e) == current_version
        and abs(_effective_composite(e) - _effective_composite(champion))
        <= _unpaired_band(e, champion, margin, dethrone_z)
        and not _shares_confirmation_seeds(e, champion)
    ]
    if not contested:
        return []
    return [champion, *contested]
