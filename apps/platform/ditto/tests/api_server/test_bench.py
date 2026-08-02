"""Unit tests for :mod:`ditto.api_server.bench`."""

from __future__ import annotations

from ditto.api_server.bench import CURRENT_BENCH_VERSION, stamp_bench_version


def test_fills_missing_version() -> None:
    assert stamp_bench_version({}) == {"bench_version": CURRENT_BENCH_VERSION}


def test_preserves_explicit_version() -> None:
    assert stamp_bench_version({"bench_version": 1}) == {"bench_version": 1}


def test_overwrites_non_int_version() -> None:
    # A bool is an int subclass but never a real version; a string is malformed.
    assert stamp_bench_version({"bench_version": True})["bench_version"] == (
        CURRENT_BENCH_VERSION
    )
    assert stamp_bench_version({"bench_version": "2"})["bench_version"] == (
        CURRENT_BENCH_VERSION
    )


def test_keeps_other_details_and_mutates_in_place() -> None:
    details = {"tokens": 10}
    result = stamp_bench_version(details)
    assert result is details
    assert result == {"tokens": 10, "bench_version": CURRENT_BENCH_VERSION}
