"""Lease-aware release drain decisions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parents[2] / "scripts" / "screener-fleet-drain.py"
_SPEC = importlib.util.spec_from_file_location("screener_fleet_drain", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_DRAIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DRAIN)


def test_idle_worker_is_ready_to_stop() -> None:
    assert _DRAIN.classify(None, now=1_000) == "ready"


def test_open_lease_with_fresh_progress_waits() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 2_000, "progress_at": 900},
            now=1_000,
        )
        == "wait"
    )


def test_quiet_stretch_inside_the_lease_is_not_an_orphan() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 5_000, "progress_at": 1},
            now=1_000,
        )
        == "wait"
    )


def test_expired_lease_without_fresh_progress_is_held_not_killed() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 1_000, "progress_at": 1},
            now=2_000,
        )
        == "held"
    )


def test_progress_past_the_deadline_keeps_waiting() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 1_000, "progress_at": 1_900},
            now=2_000,
        )
        == "wait"
    )
