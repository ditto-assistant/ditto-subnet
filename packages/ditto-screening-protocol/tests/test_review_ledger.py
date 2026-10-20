"""The shared budget-terminated ledger rules used by worker and Platform."""

from __future__ import annotations

from ditto_screening_protocol.review_ledger import (
    MULTI_LOCATION_CATEGORIES,
    concern_threshold_reached,
    substantiated_concern_count,
)


def _concern(path: str | None, line: int | None, category: str = "none") -> dict:
    return {"kind": "concern", "category": category, "path": path, "line": line}


def test_counts_distinct_cited_concern_locations_only() -> None:
    notes = [
        _concern("a.py", 1),
        _concern("a.py", 1),  # duplicate location
        _concern("a.py", 2),
        _concern(None, 3),  # uncited
        _concern("", 4),  # uncited
        {"kind": "cleared", "path": "b.py", "line": 1},
        {"kind": "observation", "path": "c.py", "line": 1},
        "not-a-note",
    ]
    assert substantiated_concern_count(notes) == 2  # type: ignore[arg-type]


def test_multi_location_categories_need_two_sites() -> None:
    category = sorted(MULTI_LOCATION_CATEGORIES)[0]
    assert substantiated_concern_count([_concern("a.py", 1, category)]) == 0
    assert (
        substantiated_concern_count(
            [_concern("a.py", 1, category), _concern("b.py", 9, category)]
        )
        == 2
    )


def test_threshold_is_fewer_than_n_does_not_hold() -> None:
    notes = [_concern("a.py", 1), _concern("a.py", 2)]
    assert concern_threshold_reached(notes, concern_hold_count=2)
    assert not concern_threshold_reached(notes, concern_hold_count=3)
    assert not concern_threshold_reached([], concern_hold_count=1)
    # Values below 1 behave as 1.
    assert concern_threshold_reached(notes[:1], concern_hold_count=0)
