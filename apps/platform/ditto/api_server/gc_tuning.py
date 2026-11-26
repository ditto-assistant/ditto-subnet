"""Cyclic garbage-collector settings for the long-running API process.

The API is one asyncio process, so a full (generation 2) collection stops every
request at once. On 2026-11-06 prod profiling saw them take 16-27% of the
process's CPU, single pauses of 2.5 s, and the heap at about 1 GB. Two settings
cut that cost without changing what is collected:

* ``gc.freeze()`` once startup has finished moves everything alive at that
  point (modules, routers, Pydantic schemas, compiled SQL) to the permanent
  generation, so a full collection no longer re-walks it.
* Higher thresholds make the young collections, and the full ones they
  cascade into, run less often. Reference counting still frees acyclic objects
  immediately; only cyclic garbage waits longer.

``DITTO_API_GC_THRESHOLDS`` overrides the thresholds as ``"gen0,gen1,gen2"``,
and ``off`` leaves the interpreter defaults and skips the freeze, so the change
can be backed out with a restart.
"""

from __future__ import annotations

import gc
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GC_THRESHOLDS_ENV = "DITTO_API_GC_THRESHOLDS"
DEFAULT_GC_THRESHOLDS: tuple[int, int, int] = (50_000, 20, 20)

_applied = False


class GcTuningConfigError(ValueError):
    """``DITTO_API_GC_THRESHOLDS`` is not ``off`` or three positive integers."""


@dataclass(frozen=True)
class GcTuning:
    thresholds: tuple[int, int, int] | None
    """``None`` means leave the interpreter's collector settings alone."""


def parse_gc_tuning_from_env(environ: dict[str, str] | None = None) -> GcTuning:
    raw = (environ if environ is not None else os.environ).get(GC_THRESHOLDS_ENV)
    if raw is None or not raw.strip():
        return GcTuning(thresholds=DEFAULT_GC_THRESHOLDS)
    value = raw.strip()
    if value.lower() == "off":
        return GcTuning(thresholds=None)
    parts = [part.strip() for part in value.split(",")]
    try:
        numbers = tuple(int(part) for part in parts)
    except ValueError as e:
        raise GcTuningConfigError(
            f"{GC_THRESHOLDS_ENV} must be 'off' or 'gen0,gen1,gen2', got {raw!r}"
        ) from e
    if len(numbers) != 3 or any(number <= 0 for number in numbers):
        raise GcTuningConfigError(
            f"{GC_THRESHOLDS_ENV} needs three positive integers, got {raw!r}"
        )
    return GcTuning(thresholds=(numbers[0], numbers[1], numbers[2]))


def apply_gc_tuning(tuning: GcTuning) -> bool:
    """Freeze the startup heap and set the thresholds, once per process.

    Returns whether anything was changed by this call.
    """
    global _applied
    if tuning.thresholds is None or _applied:
        return False
    # Collect first so startup garbage is not frozen along with live objects.
    gc.collect()
    gc.freeze()
    gc.set_threshold(*tuning.thresholds)
    _applied = True
    logger.info(
        "gc tuned: froze %d startup objects, thresholds=%s",
        gc.get_freeze_count(),
        tuning.thresholds,
    )
    return True
