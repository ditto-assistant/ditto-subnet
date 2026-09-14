"""The shared finalized-block binding constants both CRN copies import."""

from __future__ import annotations

import pytest

from ditto_screening_protocol.crn_block_binding import (
    CRN_ANCHOR_BLOCK_DELTA,
    CRN_BLOCK_BINDING_MIN_BENCH_VERSION,
    normalize_block_hash,
)


def test_floor_and_delta_are_the_v13_contract() -> None:
    # Moving either constant re-derives every bound confirmation family; the
    # validator and Platform copies import these values, so this is the one
    # place a bump is pinned.
    assert CRN_BLOCK_BINDING_MIN_BENCH_VERSION == 13
    assert CRN_ANCHOR_BLOCK_DELTA == 10


@pytest.mark.parametrize(
    "raw",
    ["0x" + "AB" * 32, "AB" * 32, "  0x" + "ab" * 32 + "\n", "0X" + "ab" * 32],
)
def test_normalization_is_prefix_and_case_insensitive(raw: str) -> None:
    assert normalize_block_hash(raw) == "ab" * 32


def test_normalization_of_an_empty_hash_is_empty() -> None:
    assert normalize_block_hash("0x") == ""
    assert normalize_block_hash("   ") == ""
