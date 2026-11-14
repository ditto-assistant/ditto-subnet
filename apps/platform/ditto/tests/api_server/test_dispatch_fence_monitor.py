"""Process-local record of what the job allocator's dispatch fence is busy with."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ditto.api_server.dispatch_fence_monitor import DispatchFenceMonitor


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 22, 0, tzinfo=UTC)
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


def _monitor(clock: _Clock) -> DispatchFenceMonitor:
    return DispatchFenceMonitor(clock=lambda: clock.now, monotonic=lambda: clock.mono)


async def test_sibling_slots_bouncing_off_their_own_host_read_as_self_collisions() -> (
    None
):
    """The storm's signature: brief holds, and busy polls from the holder's host."""
    clock = _Clock()
    monitor = _monitor(clock)
    clock.advance(600)

    attempt = monitor.attempt("5Big", "slot-0")
    async with attempt:
        attempt.acquired(lambda: "no_candidate")
        clock.advance(0.05)
        for slot in ("slot-1", "slot-2", "slot-3"):
            monitor.attempt("5Big", slot).busy()
        monitor.attempt("5Other", "slot-0").busy()
        live = monitor.snapshot()
        assert live["holder"]["validator_hotkey"] == "5Big"
        assert live["holder"]["held_seconds"] == pytest.approx(0.05)
    monitor.attempt("5Big", "slot-4").busy()

    snap = monitor.snapshot()
    assert snap["holder"] is None
    assert snap["recent_holds"] == 1
    assert snap["hold_outcomes"] == {"no_candidate": 1}
    assert snap["hold_p95_seconds"] == pytest.approx(0.05)
    assert snap["recent_utilization"] < 0.001
    assert snap["recent_busy_declines"] == 5
    assert snap["recent_busy_same_validator"] == 3
    assert snap["recent_busy_untracked_holder"] == 1
    pairs = {
        (row["validator_hotkey"], row["holder_hotkey"]): row
        for row in snap["busy_by_validator"]
    }
    assert pairs[("5Big", "5Big")]["same_validator"] is True
    assert pairs[("5Big", "5Big")]["busy_declines"] == 3
    assert pairs[("5Other", "5Big")]["same_validator"] is False
    assert pairs[("5Big", None)]["busy_declines"] == 1


async def test_a_hold_ended_by_an_exception_is_labelled_and_released() -> None:
    clock = _Clock()
    monitor = _monitor(clock)
    attempt = monitor.attempt("5Val", "slot-0")
    with pytest.raises(RuntimeError):
        async with attempt:
            attempt.acquired()
            clock.advance(2.0)
            raise RuntimeError("boom")
    snap = monitor.snapshot()
    assert snap["holder"] is None
    assert snap["hold_outcomes"] == {"error_RuntimeError": 1}
    assert snap["hold_max_seconds"] == pytest.approx(2.0)


async def test_an_attempt_that_never_acquired_records_no_hold() -> None:
    clock = _Clock()
    monitor = _monitor(clock)
    async with monitor.attempt("5Val", "slot-0") as attempt:
        attempt.busy()
    snap = monitor.snapshot()
    assert snap["recent_holds"] == 0
    assert snap["hold_p50_seconds"] is None
    assert snap["recent_busy_declines"] == 1


async def test_events_age_out_of_the_recent_window() -> None:
    clock = _Clock()
    monitor = _monitor(clock)
    attempt = monitor.attempt("5Val", "slot-0")
    async with attempt:
        attempt.acquired()
        clock.advance(1.0)
    monitor.attempt("5Val", "slot-1").busy()
    clock.advance(16 * 60)
    snap = monitor.snapshot()
    assert snap["recent_holds"] == 0
    assert snap["recent_busy_declines"] == 0
    assert snap["holds_by_validator"] == []
