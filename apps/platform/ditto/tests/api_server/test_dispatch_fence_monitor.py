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


async def test_utilization_counts_only_the_window_and_the_live_hold() -> None:
    clock = _Clock()
    monitor = _monitor(clock)
    clock.advance(3600)
    # A 120s hold whose first 60s fall before the window opens at read time
    # (read 900s after the hold's 60th second): only its last 60s count.
    attempt = monitor.attempt("5Val", "slot-0")
    async with attempt:
        attempt.acquired()
        clock.advance(120)
    clock.advance(900 - 60 - 30)
    live = monitor.attempt("5Val", "slot-1")
    async with live:
        live.acquired()
        clock.advance(30)
        snap = monitor.snapshot()
        # 60s of the clipped hold plus 30s of the hold still in progress.
        assert snap["recent_held_seconds"] == pytest.approx(90.0)
        assert snap["recent_utilization"] == pytest.approx(90.0 / 900)
        # Quantiles keep the completed hold's full duration.
        assert snap["hold_max_seconds"] == pytest.approx(120.0)


async def test_busy_is_attributed_to_the_holder_seen_before_the_lock_check() -> None:
    """The holder that refused the poll may release before the busy is recorded."""
    clock = _Clock()
    monitor = _monitor(clock)
    holder = monitor.attempt("5Holder", "slot-0")
    contender = monitor.attempt("5Contender", "slot-3")
    async with holder:
        holder.acquired()
        contender.before_lock_check()
        clock.advance(0.01)
    # The round trip finished after the holder committed.
    contender.busy()
    snap = monitor.snapshot()
    assert snap["recent_busy_untracked_holder"] == 0
    assert snap["busy_by_validator"][0]["holder_hotkey"] == "5Holder"


async def test_a_failing_outcome_label_still_releases_the_hold() -> None:
    clock = _Clock()
    monitor = _monitor(clock)

    def broken() -> str:
        raise RuntimeError("label")

    attempt = monitor.attempt("5Val", "slot-0")
    async with attempt:
        attempt.acquired(broken)
        clock.advance(0.5)
    snap = monitor.snapshot()
    assert snap["holder"] is None
    assert snap["hold_outcomes"] == {"unlabelled": 1}


async def test_live_hold_uses_monotonic_time_across_a_wall_clock_jump() -> None:
    clock = _Clock()
    monitor = _monitor(clock)
    clock.advance(1000)
    attempt = monitor.attempt("5Val", "slot-0")
    async with attempt:
        attempt.acquired()
        clock.mono += 10
        # The wall clock steps back an hour mid-hold.
        clock.now -= timedelta(hours=1)
        snap = monitor.snapshot()
        assert snap["recent_held_seconds"] == pytest.approx(10.0)
        assert snap["holder"]["held_seconds"] == pytest.approx(10.0)
