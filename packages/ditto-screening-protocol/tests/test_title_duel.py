"""Shared title-duel arithmetic (protocol 35, docs/title-duels.md)."""

from __future__ import annotations

import pytest

from ditto_screening_protocol.title_duel import (
    TITLE_DUEL_BOUNDARY,
    TITLE_DUEL_WINDOW_PAIRS,
    collapse_records,
    duel_pairs,
    duel_required_lead,
    duel_statistic,
    season_seed_key,
)


def test_boundary_is_the_frozen_pocock_table() -> None:
    assert sorted(TITLE_DUEL_BOUNDARY) == list(range(3, TITLE_DUEL_WINDOW_PAIRS + 1))
    values = [TITLE_DUEL_BOUNDARY[n] for n in sorted(TITLE_DUEL_BOUNDARY)]
    assert values == sorted(values, reverse=True)
    # t_{n-1}^{-1}(0.99): 6.965 at two degrees of freedom, 2.624 at fourteen.
    assert TITLE_DUEL_BOUNDARY[3] == 6.965
    assert TITLE_DUEL_BOUNDARY[15] == 2.624


def test_records_collapse_to_one_median_per_season_seed() -> None:
    collapsed = collapse_records(
        [
            (season_seed_key("s1", 1, 7), 0.6),
            (season_seed_key("s1", 1, 7), 0.8),
            (season_seed_key("s1", 1, 7), 0.7),
            (season_seed_key("s2", 2, 7), 0.5),
            # Disagreeing season indices for one pair: dropped, never guessed.
            (season_seed_key("s3", 3, 9), 0.5),
            (season_seed_key("s3", 4, 9), 0.5),
        ]
    )
    assert collapsed == {("s1", 7): (1, 0.7), ("s2", 7): (2, 0.5)}


def test_window_keeps_the_newest_shared_pairs() -> None:
    a = {("s1", 1): (1, 0.9), ("s2", 2): (2, 0.8), ("s3", 3): (3, 0.7)}
    b = {
        ("s1", 1): (1, 0.5),
        ("s2", 2): (2, 0.5),
        ("s3", 3): (3, 0.5),
        ("s4", 4): (4, 0.5),
    }
    pairs = duel_pairs(a, b, window=2)
    assert [(p.season_index, p.seed) for p in pairs] == [(3, 3), (2, 2)]


def test_statistic_and_required_lead() -> None:
    a = {(f"s{i}", i): (i, 0.7 + d) for i, d in enumerate((0.01, 0.03, 0.02), 1)}
    b = {(f"s{i}", i): (i, 0.7) for i in range(1, 4)}
    stat = duel_statistic(duel_pairs(a, b))
    assert stat is not None and stat.count == 3
    assert stat.mean_difference == pytest.approx(0.02)
    assert stat.standard_error == pytest.approx(0.01 / 3**0.5)
    lead = duel_required_lead(stat, margin_floor=0.007)
    assert lead == pytest.approx(6.965 * 0.01 / 3**0.5)
    two = duel_statistic(duel_pairs(dict(list(a.items())[:2]), b))
    assert two is not None and duel_required_lead(two, margin_floor=0.007) is None
    assert duel_statistic(duel_pairs(dict(list(a.items())[:1]), b)) is None
