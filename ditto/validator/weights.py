"""KOTH + ATH-gate weight function: map the score ledger to a weight vector.

The incentive mechanism (``docs/incentive-mechanism.md`` Option A): the reigning
all-time-high holder is the **champion** and takes ~all emissions; a challenger
only dethrones it by beating its score by a **relative margin** (default 1%);
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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto.api_models.validator import LedgerEntry

# Benchmark scores are only comparable within one bench_version: a version bump
# changes what the composite means, so folding a v2 champion against a v3
# challenger would be nonsense (BENCHMARK-V2.md §9). Entries whose bench_version
# the platform does not (yet) surface are treated as this baseline — so on a
# ledger with no version info the fold is unchanged (all one version).
DEFAULT_BENCH_VERSION = 1


def _entry_version(entry: LedgerEntry) -> int:
    """The entry's bench_version, or DEFAULT_BENCH_VERSION when the platform
    ledger does not carry one. Read via getattr so the wire model can stay
    untouched (the platform surfacing bench_version on the ledger is optional
    per BENCHMARK-V2 §7 — until then this is a safe no-op)."""
    v = getattr(entry, "bench_version", None)
    return v if isinstance(v, int) and v > 0 else DEFAULT_BENCH_VERSION


def max_bench_version(entries: Sequence[LedgerEntry]) -> int:
    """The newest bench_version present in the ledger."""
    return max((_entry_version(e) for e in entries), default=DEFAULT_BENCH_VERSION)


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
) -> dict[str, float]:
    """Return ``{miner_hotkey: weight}`` for the KOTH+ATH mechanism.

    ``entries`` is the ledger: one best-scoring agent per miner. The champion is
    found by folding entries in **first-seen order** (``first_seen`` then
    ``agent_id`` to break timestamp ties) and dethroning the running champion
    only when a later entry's composite exceeds it by ``margin`` relative. The
    champion gets ``champion_share``; the next ``tail_size`` miners by composite
    split ``1 - champion_share`` equally.

    Only entries at the **max bench_version present** are folded (§9 step 3):
    scores under an older benchmark version are incomparable and dropped until a
    re-score lifts them to the current version.

    Non-positive composites are dropped (a zero-scoring miner earns nothing).
    Returns an empty dict when no miner scored above zero — the caller then skips
    ``put_weights`` rather than zeroing the chain. Pylon/Subtensor normalizes the
    returned vector, so only the ratios matter; when there is no tail the champion
    is the whole vector.
    """
    scored = [e for e in filter_to_latest_version(entries) if e.composite > 0.0]
    if not scored:
        return {}

    # Champion: fold in creation order; a later entry must beat the running
    # champion by the relative margin to take the crown. Order-independent of
    # when each agent happened to be scored — only creation order matters.
    champion = _champion(scored, margin)
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


def _champion(entries: Sequence[LedgerEntry], margin: float) -> LedgerEntry:
    """The KOTH champion of a positive-composite entry set: fold in first-seen
    order, dethroning only on a > relative-margin beat."""
    ordered = sorted(entries, key=lambda e: (e.first_seen, e.agent_id))
    champ = ordered[0]
    for e in ordered[1:]:
        if e.composite > champ.composite * (1.0 + margin):
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
) -> list[LedgerEntry]:
    """The champion + participation-tail entries scored under an **older**
    bench_version than ``current_version`` — they must be re-evaluated before the
    fold can compare them at the new version (BENCHMARK-V2 §9 step 2).

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
    champion = _champion(scored, margin)
    rewarded = [champion, *_tail(scored, champion, tail_size)]
    return [e for e in rewarded if _entry_version(e) < current_version]
