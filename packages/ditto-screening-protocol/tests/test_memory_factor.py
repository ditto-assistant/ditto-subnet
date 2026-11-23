"""The Python memory-factor curve must match the Go scorer's (SN-500)."""

from __future__ import annotations

import pytest

from ditto_screening_protocol.memory_factor import (
    DEFAULT_MEMORY_FACTOR_FLOOR_BPS,
    DEFAULT_MEMORY_FACTOR_FREE_MIB,
    is_memory_factor_bench_version,
    memory_factor_bps,
)

MIB = 1 << 20
FREE = DEFAULT_MEMORY_FACTOR_FREE_MIB * MIB
CAP = 2048 * MIB


# The same vectors as TestMemoryFactorCurve in
# services/dittobench-api/internal/scoregates/memory_factor_test.go.
@pytest.mark.parametrize(
    ("p95_mib", "expected"),
    [(175, 10_000), (512, 10_000), (896, 9_750), (1280, 9_500), (2048, 9_000), (4096, 9_000)],
)
def test_curve_matches_the_go_vectors(p95_mib: int, expected: int) -> None:
    assert (
        memory_factor_bps(p95_mib * MIB, FREE, CAP, DEFAULT_MEMORY_FACTOR_FLOOR_BPS)
        == expected
    )


def test_a_cap_inside_the_allowance_has_no_penalised_band() -> None:
    assert memory_factor_bps(900 * MIB, 1024 * MIB, 863 * MIB, 9_000) == 10_000


def test_rounds_half_away_from_zero_like_go() -> None:
    assert memory_factor_bps(3, 2, 4, 9_000) == 9_500
    # drop = 1000 x 1 / 2000 = 0.5 -> Go math.Round(0.5) = 1, not banker's 0.
    assert memory_factor_bps(1, 0, 2_000, 9_000) == 9_999


def test_bench_floor() -> None:
    assert not is_memory_factor_bench_version(None)
    assert not is_memory_factor_bench_version(15)
    assert is_memory_factor_bench_version(16)
    assert is_memory_factor_bench_version(18)
