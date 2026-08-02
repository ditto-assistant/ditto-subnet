"""Benchmark-version constants shared across the API surface.

``CURRENT_BENCH_VERSION`` is the latest DittoBench benchmark version (mirrors the
scorer's ``protocol.BenchVersion`` in dittobench-api). It is the single source of
truth for "which benchmark is current":

- the public leaderboard reports it (``current_bench_version``) so the dashboard
  can mark any entry with a lower ``bench_version`` as a previous-benchmark run,
  and a run with *no* recorded version as a pre-versioning **legacy** run;
- the validator score-ingest path stamps it onto any report that omits a version
  (:func:`stamp_bench_version`), so no run scored *from now on* is ever recorded
  as legacy — only genuine historical rows keep a null version.

Bump this when a new benchmark ships (and update the scorer to emit it).
"""

from __future__ import annotations

from typing import Any

from ditto.api_models.benchmark_contract import latest_benchmark_contract

# The current DittoBench benchmark version. See module docstring.
#
# Tracks the version being rolled OUT, not the one currently authoritative for
# weights -- it was 3 throughout the 2->3 rollout while active_version was still
# 2. The newest shipped immutable contract is discovery metadata; it does not
# open a rollout or change the active weight authority. Rollout selection is a
# separate authenticated operator action backed by the durable rollout row.
CURRENT_BENCH_VERSION = latest_benchmark_contract().version


def is_bench_version_retired(version: int, active_version: int) -> bool:
    """Whether ``version`` is a superseded (retired) benchmark.

    A version is retired once a newer one ships (``version < CURRENT``). Its
    datasets are never scored again, so their full labeled corpus (answer keys
    included) is safe to release publicly with zero anti-overfit cost. The current
    (live) version and any unknown future version are NOT retired.
    """
    return 0 < version < active_version


def stamp_bench_version(details: dict[str, Any]) -> dict[str, Any]:
    """Ensure a score-report details blob carries a valid ``bench_version``.

    Fills ``bench_version`` with :data:`CURRENT_BENCH_VERSION` only when the
    scorer left it absent or non-integer (``bool`` is rejected — it is an ``int``
    subclass but never a real version). An explicit integer version is left
    untouched: a report that genuinely ran an older benchmark stays honestly
    labelled with that version rather than being silently bumped. The result is
    that new runs are never "legacy" (null version) while real provenance is
    preserved. Mutates and returns ``details`` for convenience.
    """
    v = details.get("bench_version")
    if not isinstance(v, int) or isinstance(v, bool):
        details["bench_version"] = CURRENT_BENCH_VERSION
    return details
