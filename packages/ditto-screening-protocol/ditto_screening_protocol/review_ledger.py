"""Shared rules for a budget-terminated source review's notes ledger.

The screener worker decides a budget-terminated review from the notes it
recorded (``ledger_disposition``), and Platform must describe that same hold
publicly without contradicting it. Both import these rules from here so the
worker's hold decision and Platform's public conclusion cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Finding categories whose claims need two distinct source locations to be
# admissible, so a single-site concern in them can never become a finding.
MULTI_LOCATION_CATEGORIES = frozenset(
    {"benchmark_emulation", "scorer_contract_manipulation"}
)


def substantiated_concern_count(
    notes: Sequence[Mapping[str, object]],
) -> int:
    """Count concerns that could actually survive as a finding.

    A ``concern`` note is a lead the reviewer recorded mid-inspection, not a
    verdict. The reviewer records them liberally by design -- the prompt asks
    for one "the moment you see one" -- so counting them raw makes any
    budget-cut review look guilty. Production, 2026-08-28: every one of 273
    concern notes cited a path, so "did it cite" separates nothing; distinct
    locations do. Reviews that RAN TO COMPLETION and then concluded low risk
    carried at most 2 substantiated concerns (mean 0.8), while budget- or
    fault-terminated reviews carried 6 to 19.

    The multi-location rule is the finding contract itself: a
    ``benchmark_emulation`` or ``scorer_contract_manipulation`` claim needs two
    distinct source locations to be admissible as a finding, so a single-site
    note in those categories could never have become one either.
    """
    locations: dict[str, set[tuple[str, object]]] = {}
    for note in notes:
        if not isinstance(note, Mapping) or note.get("kind") != "concern":
            continue
        path = note.get("path")
        if not isinstance(path, str) or not path:
            continue
        category = str(note.get("category") or "none")
        locations.setdefault(category, set()).add((path, note.get("line")))
    total = 0
    for category, sites in locations.items():
        if category in MULTI_LOCATION_CATEGORIES and len(sites) < 2:
            continue
        total += len(sites)
    return total


def concern_threshold_reached(
    notes: Sequence[Mapping[str, object]], *, concern_hold_count: int
) -> bool:
    """True when the recorded concerns alone hold a budget-terminated review.

    ``concern_hold_count`` means "fewer than this many substantiated concerns
    does not hold"; values below 1 are treated as 1.
    """
    return substantiated_concern_count(notes) >= max(1, concern_hold_count)
