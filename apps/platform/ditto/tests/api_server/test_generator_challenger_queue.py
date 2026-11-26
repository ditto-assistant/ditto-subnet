"""Unit tests for the challenger queue's template families and pick order."""

from __future__ import annotations

import hashlib
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
    prescreen: float | None = None,
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
        prescreen_score=prescreen,
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


def _probe(*tags: str) -> list[str]:
    return [hashlib.sha256(tag.encode()).hexdigest() for tag in tags]


def test_within_a_position_the_more_novel_generator_goes_first() -> None:
    """A partial copy of a measured generator's probe cases waits behind a
    generator whose cases are new, even though it qualified first."""
    measured = _gen("measured", owner="m", minutes=-60, module="m", score=0.3)
    partial = _gen("partial", owner="p", minutes=0, module="p")
    novel = _gen("novel", owner="n", minutes=10, module="n")
    probes = {
        measured.submission_id: _probe("m1", "m2", "m3", "m4"),
        partial.submission_id: _probe("m1", "m2", "p3", "p4"),
        novel.submission_id: _probe("n1", "n2", "n3", "n4"),
    }
    entries = challenger_queue([measured, partial, novel], {}, probes)
    assert [(e.row.miner_hotkey[1:], e.novelty) for e in entries] == [
        ("novel", 1.0),
        ("partial", 0.5),
    ]


def test_a_mosaic_of_measured_cases_counts_as_known() -> None:
    """Cases copied from two measured generators, each well under the probe
    copy threshold, add up: novelty counts every known case."""
    a = _gen("a", owner="a", minutes=-60, module="a", score=0.3)
    b = _gen("b", owner="b", minutes=-50, module="b", score=0.2)
    mosaic = _gen("mosaic", owner="x", minutes=0, module="x")
    fresh = _gen("fresh", owner="y", minutes=10, module="y")
    probes = {
        a.submission_id: _probe("a1", "a2", "a3", "a4"),
        b.submission_id: _probe("b1", "b2", "b3", "b4"),
        mosaic.submission_id: _probe("a1", "a2", "b1", "x4"),
        fresh.submission_id: _probe("y1", "y2", "y3", "y4"),
    }
    entries = challenger_queue([a, b, mosaic, fresh], {}, probes)
    assert [(e.row.miner_hotkey[1:], e.novelty) for e in entries] == [
        ("fresh", 1.0),
        ("mosaic", 0.25),
    ]


def test_identical_builds_wait_behind_a_build_with_new_cases() -> None:
    """One coldkey's identical builds emit one probe output (prod, Nov 11:
    eight kinbind builds, pairwise probe overlap 1.0). Once one is ahead in
    the queue the rest add nothing new, so a build with different cases from
    the same owner goes first."""
    kinbind1 = _gen("kinbind1", owner="kin", minutes=0, module="kinbind")
    kinbind2 = _gen("kinbind2", owner="kin", minutes=1, module="kinbind")
    kinlist = _gen("kinlist", owner="kin", minutes=2, module="kinlist", files=47)
    probes = {
        kinbind1.submission_id: _probe("k1", "k2"),
        kinbind2.submission_id: _probe("k1", "k2"),
        kinlist.submission_id: _probe("l1", "l2"),
    }
    entries = challenger_queue([kinbind1, kinbind2, kinlist], {}, probes)
    assert [
        (e.row.miner_hotkey[1:], e.family_position, e.novelty) for e in entries
    ] == [("kinbind1", 0, 1.0), ("kinlist", 1, 1.0), ("kinbind2", 2, 0.0)]


def test_family_position_still_comes_before_novelty() -> None:
    """A wholly novel second build of a family waits behind another family's
    first build, even one half made of known cases."""
    measured = _gen("measured", owner="m", minutes=-60, module="m", score=0.3)
    second = _gen("second", owner="m", minutes=0, module="m2")
    head = _gen("head", owner="h", minutes=5, module="h")
    probes = {
        measured.submission_id: _probe("x1", "x2"),
        second.submission_id: _probe("s1", "s2"),
        head.submission_id: _probe("x1", "h2"),
    }
    entries = challenger_queue([measured, second, head], {}, probes)
    assert [
        (e.row.miner_hotkey[1:], e.family_position, e.novelty) for e in entries
    ] == [
        ("head", 0, 0.5),
        ("second", 1, 1.0),
    ]


def test_a_generator_without_a_probe_result_counts_as_novel() -> None:
    measured = _gen("measured", owner="m", minutes=-60, module="m", score=0.3)
    unprobed = _gen("unprobed", owner="u", minutes=0, module="u")
    repeats = _gen("repeats", owner="r", minutes=5, module="r")
    probes = {
        measured.submission_id: _probe("a", "b"),
        # A repeated fingerprint counts once: half of its distinct cases are new.
        repeats.submission_id: _probe("a", "a", "c"),
    }
    entries = challenger_queue([measured, unprobed, repeats], {}, probes)
    assert [(e.row.miner_hotkey[1:], e.novelty) for e in entries] == [
        ("unprobed", 1.0),
        ("repeats", 0.5),
    ]


def test_an_in_flight_entry_counts_its_family_members_queued_ahead() -> None:
    rows = [
        _gen("kin1", owner="kin", minutes=0),
        _gen("kin2", owner="kin", minutes=1),
        _gen("kin3", owner="kin", minutes=2),
    ]
    entries = challenger_queue(rows, {rows[0].submission_id: 26113})
    assert [(e.row.miner_hotkey[1:], e.family_position) for e in entries] == [
        ("kin2", 1),
        ("kin3", 2),
        ("kin1", 3),
    ]


def test_prescreen_results_promote_within_a_family_position() -> None:
    """A positive pre-screen score goes first (best first), then the
    generators not pre-screened yet, then one whose pre-screen scored zero."""
    unscreened = _gen("unscreened", owner="u", minutes=0, module="u")
    good = _gen("good", owner="g", minutes=1, module="g", prescreen=0.3)
    best = _gen("best", owner="b", minutes=2, module="b", prescreen=0.6)
    failed = _gen("failed", owner="f", minutes=3, module="f", prescreen=0.0)
    assert _order([unscreened, good, best, failed]) == [
        "best",
        "good",
        "unscreened",
        "failed",
    ]


def test_family_position_still_comes_before_a_prescreen_score() -> None:
    """A batch of builds that all pre-screen well still cannot take every
    challenger column: a measured family's next build waits behind another
    family's first."""
    measured = _gen("measured", owner="k", minutes=-60, module="k", score=0.2)
    sibling = _gen("sibling", owner="k", minutes=0, module="k2", prescreen=0.9)
    newcomer = _gen("newcomer", owner="n", minutes=5, module="n")
    assert _order([measured, sibling, newcomer]) == ["newcomer", "sibling"]
