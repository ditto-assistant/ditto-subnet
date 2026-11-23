"""The P95 harness-memory factor curve (SN-500 step 2), mirrored from Go.

The scorer (``services/dittobench-api/internal/scoregates/memory_factor.go``)
maps a run's time-weighted P95 harness memory to a composite factor and
records it as ``details.memory_factor``. This module is the same curve so
Platform can re-derive a reported factor and a backtest can apply it:

    P95 <= free           -> 10000 bps
    free < P95 < cap      -> 10000 - (10000 - floor) x (P95 - free) / (cap - free)
    P95 >= cap            -> floor

rounded half away from zero, like Go's ``math.Round``.
"""

from __future__ import annotations

import math

MEMORY_FACTOR_MIN_BENCH_VERSION = 16
DEFAULT_MEMORY_FACTOR_FREE_MIB = 512
DEFAULT_MEMORY_FACTOR_FLOOR_BPS = 9_000
_BPS = 10_000


def is_memory_factor_bench_version(bench_version: int | None) -> bool:
    """Whether ``bench_version`` records ``details.memory_factor``."""
    return bench_version is not None and bench_version >= MEMORY_FACTOR_MIN_BENCH_VERSION


def memory_factor_bps(
    p95_bytes: int, free_bytes: int, limit_bytes: int, floor_bps: int
) -> int:
    """The factor in basis points for one run's P95 under the curve above."""
    if p95_bytes <= free_bytes or limit_bytes <= free_bytes:
        return _BPS
    span = limit_bytes - free_bytes
    over = min(p95_bytes - free_bytes, span)
    drop = (_BPS - floor_bps) * over / span
    return _BPS - int(math.floor(drop + 0.5))


__all__ = [
    "DEFAULT_MEMORY_FACTOR_FLOOR_BPS",
    "DEFAULT_MEMORY_FACTOR_FREE_MIB",
    "MEMORY_FACTOR_MIN_BENCH_VERSION",
    "is_memory_factor_bench_version",
    "memory_factor_bps",
]
