from __future__ import annotations

import pytest

from ditto_edge_protocol.ledger import (
    HoldInterval,
    LedgerParams,
    Observation,
    hypothetical_shares,
    score_window,
)
from ditto_edge_protocol.limits import GIB

START, END = 0.0, 3600.0
PARAMS = LedgerParams(min_challenges=3, availability_floor=0.8)


def _score(observations: list[Observation], holds: list[HoldInterval] | None = None):
    return score_window(
        observations=observations,
        holds=holds if holds is not None else [HoldInterval(GIB, START, None)],
        window_start=START,
        window_end=END,
        params=PARAMS,
    )


def _obs(outcome: str, n: int = 1, purpose: str = "possession") -> list[Observation]:
    return [Observation(purpose, outcome)] * n  # type: ignore[arg-type]


def test_honest_full_window_earns_held_gib_hours() -> None:
    score = _score(_obs("passed", 6))
    assert score.eligible
    assert score.availability == 1.0
    assert score.held_gib_hours == 1.0
    assert score.verified_units == 1.0
    assert score.reasons == ()


def test_offline_half_the_window_halves_availability_and_fails_the_floor() -> None:
    score = _score(_obs("passed", 3) + _obs("missed", 3))
    assert score.availability == 0.5
    assert not score.eligible
    assert score.reasons == ("availability_below_floor",)
    assert score.verified_units == 0.0


def test_refills_count_against_availability() -> None:
    score = _score(_obs("refilled", 4) + _obs("passed", 1))
    assert score.refilled == 4
    assert score.availability == 0.2
    assert not score.eligible


def test_one_integrity_failure_zeroes_the_window() -> None:
    score = _score(_obs("passed", 9) + _obs("failed", 1, "retrieval"))
    assert score.failed == 1
    assert "integrity_failure" in score.reasons
    assert score.verified_units == 0.0


def test_answering_for_revoked_content_zeroes_the_window() -> None:
    score = _score(_obs("passed", 6) + _obs("violation", 1, "revoked"))
    assert score.revocation_violations == 1
    assert score.revocation_checks == 1
    assert score.reasons == ("revocation_violation",)


def test_revocation_checks_do_not_count_toward_availability() -> None:
    score = _score(_obs("passed", 4) + _obs("refused_ok", 2, "revoked"))
    assert score.scheduled == 4
    assert score.revocation_checks == 2
    assert score.availability == 1.0
    assert score.eligible


def test_revocation_checks_count_toward_the_minimum() -> None:
    # Two possession passes alone would be too few; two refused revoked
    # objects show the node was online and honest for the rest.
    score = _score(_obs("passed", 2) + _obs("refused_ok", 2, "revoked"))
    assert score.scheduled == 2
    assert score.eligible
    assert score.reasons == ()


def test_a_missed_revocation_check_is_a_miss_not_a_violation() -> None:
    score = _score(_obs("passed", 4) + _obs("missed", 1, "revoked"))
    assert score.revocation_violations == 0
    assert score.missed == 1
    assert score.availability == 0.8
    assert score.eligible


def test_pending_after_close_counts_as_missed_and_void_is_ignored() -> None:
    score = _score(_obs("passed", 3) + _obs("pending", 1) + _obs("void", 5))
    assert score.missed == 1
    assert score.scheduled == 4


def test_too_few_challenges_and_nothing_held() -> None:
    score = _score(_obs("passed", 2), holds=[])
    assert score.reasons == ("too_few_challenges", "nothing_held")
    assert not score.eligible


def test_hold_intervals_are_clipped_to_the_window() -> None:
    holds = [
        HoldInterval(GIB, -1000.0, 1800.0),  # first half
        HoldInterval(2 * GIB, 2700.0, None),  # last quarter
        HoldInterval(GIB, 4000.0, None),  # after the window
        HoldInterval(GIB, -500.0, -100.0),  # before the window
    ]
    score = _score(_obs("passed", 5), holds)
    assert score.held_gib_hours == pytest.approx(1.0)  # 0.5 + 0.5
    assert score.verified_units == pytest.approx(1.0)


def test_hypothetical_shares_normalize_positive_units() -> None:
    assert hypothetical_shares({"a": 3.0, "b": 1.0, "c": 0.0}) == {
        "a": 0.75,
        "b": 0.25,
        "c": 0.0,
    }
    assert hypothetical_shares({"a": 0.0}) == {"a": 0.0}
    assert hypothetical_shares({}) == {}
