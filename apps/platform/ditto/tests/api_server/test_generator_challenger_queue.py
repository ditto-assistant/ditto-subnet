"""Unit tests for the challenger queue's template families and pick order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from ditto.api_server.generator_challenger_queue import (
    challenger_queue,
    same_template,
    template_families,
)
from ditto.db.models import GeneratorSubmission

_NOW = datetime(2026, 11, 11, 15, tzinfo=UTC)


def _gen(
    name: str,
    *,
    owner: str,
    minutes: int,
    module: str = "example.com/gen",
    files: int = 46,
    size: int = 200_000,
    score: float | None = None,
) -> GeneratorSubmission:
    qualified = _NOW + timedelta(minutes=minutes)
    return GeneratorSubmission(
        submission_id=uuid5(NAMESPACE_URL, name),
        miner_hotkey=f"5{name}",
        owner_key=f"coldkey:{owner}",
        module=module,
        file_count=files,
        size_bytes=size,
        qualified_at=qualified,
        submitted_at=qualified - timedelta(minutes=1),
        round_score=score,
        round_scored_index=None if score is None else 1,
    )


def _order(rows: list[GeneratorSubmission], in_flight=None) -> list[str]:
    return [
        entry.row.miner_hotkey[1:] for entry in challenger_queue(rows, in_flight or {})
    ]


def test_one_owners_batch_is_interleaved_with_other_families() -> None:
    """Ten builds from one coldkey no longer take ten rounds of challenger
    columns ahead of everything qualified after the first build."""
    rows = [
        _gen("kin1", owner="kin", minutes=0, module="kinbind"),
        _gen("kin2", owner="kin", minutes=1, module="kinbind"),
        _gen("kin3", owner="kin", minutes=2, module="kinlist", files=47),
        _gen("dr", owner="dr", minutes=10, module="gen-v3h", size=486_590),
        _gen("solo", owner="solo", minutes=20, module="querverweis"),
    ]
    assert _order(rows) == ["kin1", "dr", "solo", "kin2", "kin3"]
    entries = {
        entry.row.miner_hotkey[1:]: entry for entry in challenger_queue(rows, {})
    }
    assert entries["kin3"].family == str(rows[0].submission_id)
    assert entries["kin3"].family_size == 3
    assert [entries[name].family_position for name in ("kin1", "kin2", "kin3")] == [
        0,
        1,
        2,
    ]


def test_a_measured_family_waits_behind_unmeasured_families() -> None:
    rows = [
        _gen("kinfolk", owner="kin", minutes=-60, module="kinfolk", score=0.02),
        _gen("kin1", owner="kin", minutes=0, module="kinbind"),
        _gen("dr", owner="dr", minutes=10, module="gen-v3h", size=486_590),
    ]
    assert _order(rows) == ["dr", "kin1"]
    entries = challenger_queue(rows, {})
    assert [entry.family_measured for entry in entries] == [0, 1]
    assert [entry.family_position for entry in entries] == [0, 1]


def test_builds_of_one_template_under_other_owners_share_a_family() -> None:
    clone_a = _gen("a", owner="a", minutes=0, module="reaxgen", files=124, size=852_587)
    clone_b = _gen("b", owner="b", minutes=1, module="reaxgen", files=124, size=852_585)
    bigger = _gen("c", owner="c", minutes=2, module="reaxgen", files=146, size=917_294)
    fork = _gen(
        "fork", owner="d", minutes=3, module="starter-kit", files=46, size=178_807
    )
    other_fork = _gen(
        "fork2", owner="e", minutes=4, module="starter-kit", files=47, size=190_545
    )
    assert same_template(clone_a, clone_b)
    assert not same_template(clone_a, bigger)  # another file count and size
    # One module path is not a family: unrelated miners keep the kit's path.
    assert not same_template(fork, other_fork)
    families = template_families([clone_a, clone_b, bigger, fork, other_fork])
    assert families[clone_b.submission_id] == str(clone_a.submission_id)
    assert len(set(families.values())) == 4
    assert _order([clone_a, clone_b, bigger, fork, other_fork]) == [
        "a",
        "c",
        "fork",
        "fork2",
        "b",
    ]


def test_the_size_tolerance_is_one_percent_of_the_larger_archive() -> None:
    base = _gen("base", owner="a", minutes=0, size=200_000)
    assert same_template(base, _gen("near", owner="b", minutes=1, size=202_000))
    assert not same_template(base, _gen("far", owner="c", minutes=2, size=202_100))
    assert not same_template(base, _gen("mod", owner="d", minutes=3, module="other"))


def test_an_in_flight_generator_is_measured_and_never_picked() -> None:
    rows = [
        _gen("kin1", owner="kin", minutes=0),
        _gen("kin2", owner="kin", minutes=1),
        _gen("dr", owner="dr", minutes=10, module="gen-v3h"),
    ]
    entries = challenger_queue(rows, {rows[0].submission_id: 26113})
    assert [entry.row.miner_hotkey[1:] for entry in entries] == ["dr", "kin2", "kin1"]
    assert [entry.in_flight_round for entry in entries] == [None, None, 26113]
    # kin1 is being measured, so kin2 is its family's second measurement.
    assert entries[1].family_measured == 1 and entries[1].family_position == 1


def test_the_family_key_is_its_earliest_member_whatever_the_input_order() -> None:
    rows = [
        _gen("late", owner="a", minutes=30),
        _gen("early", owner="b", minutes=0),
        _gen("middle", owner="a", minutes=15, module="other"),
    ]
    # "late" joins "early" as one template and "middle" through its owner.
    expected = str(rows[1].submission_id)
    for ordering in (rows, list(reversed(rows)), [rows[2], rows[0], rows[1]]):
        assert set(template_families(ordering).values()) == {expected}
