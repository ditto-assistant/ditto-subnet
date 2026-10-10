"""A fail-closed collector stop must be loud, not a row nobody reads (#2704).

No database: the staleness classification is pure, and the sweep loop is driven
through an in-memory cursor so the transition logic is covered on every run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY

from ditto.api_server.source_emission_collector import (
    SOURCE_EMISSION_COLLECTOR_STALL_SECONDS,
    SourceEmissionCollector,
    collector_staleness,
)

_NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
_LOGGER = "ditto.api_server.source_emission_collector"
_REASON = "RuntimeError: unaudited runtime fingerprint 0xv468"


def _stalled_gauge() -> float | None:
    return REGISTRY.get_sample_value("ditto_source_emission_collector_stalled")


def _lag_gauge() -> float | None:
    return REGISTRY.get_sample_value(
        "ditto_source_emission_collector_cursor_lag_seconds"
    )


def test_missing_cursor_is_unknown_never_stalled() -> None:
    staleness = collector_staleness(
        cursor_updated_at=None, blocked_reason=_REASON, now=_NOW
    )
    assert staleness.cursor_updated_at is None
    assert staleness.lag_seconds is None
    assert staleness.stalled is False
    assert staleness.threshold_seconds == SOURCE_EMISSION_COLLECTOR_STALL_SECONDS


def test_current_cursor_with_reason_is_not_stalled() -> None:
    """One failed sweep (or a reset reason on an advance) is not a stall."""
    staleness = collector_staleness(
        cursor_updated_at=_NOW - timedelta(seconds=45),
        blocked_reason=_REASON,
        now=_NOW,
    )
    assert staleness.lag_seconds == 45
    assert staleness.stalled is False


def test_stale_cursor_without_reason_reports_lag_only() -> None:
    staleness = collector_staleness(
        cursor_updated_at=_NOW - timedelta(hours=2),
        blocked_reason=None,
        now=_NOW,
    )
    assert staleness.lag_seconds == 7200
    assert staleness.stalled is False


def test_stale_cursor_with_reason_is_stalled_past_the_threshold_only() -> None:
    at_threshold = collector_staleness(
        cursor_updated_at=_NOW
        - timedelta(seconds=SOURCE_EMISSION_COLLECTOR_STALL_SECONDS),
        blocked_reason=_REASON,
        now=_NOW,
    )
    assert at_threshold.stalled is False
    past = collector_staleness(
        cursor_updated_at=_NOW
        - timedelta(seconds=SOURCE_EMISSION_COLLECTOR_STALL_SECONDS + 1),
        blocked_reason=_REASON,
        now=_NOW,
    )
    assert past.stalled is True
    # The #2703 shape: blocked for days.
    days = collector_staleness(
        cursor_updated_at=_NOW - timedelta(days=13),
        blocked_reason=_REASON,
        now=_NOW,
    )
    assert days.stalled is True
    assert days.lag_seconds == 13 * 86400


def test_threshold_is_overridable_and_clock_skew_clamps_to_zero() -> None:
    assert collector_staleness(
        cursor_updated_at=_NOW - timedelta(seconds=61),
        blocked_reason=_REASON,
        now=_NOW,
        threshold_seconds=60,
    ).stalled
    future = collector_staleness(
        cursor_updated_at=_NOW + timedelta(seconds=5),
        blocked_reason=_REASON,
        now=_NOW,
    )
    assert future.lag_seconds == 0
    assert future.stalled is False


class _Session:
    """Just enough AsyncSession for the collector's status write and cursor read."""

    def __init__(self, cursor: SimpleNamespace) -> None:
        self.cursor = cursor

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    def begin(self) -> _Session:
        return self

    async def execute(self, statement: Any) -> None:
        params = statement.compile().params
        self.cursor.last_blocked_reason = params["last_blocked_reason"]

    async def get(self, _model: Any, _netuid: int) -> SimpleNamespace:
        return self.cursor


_APP_STATE = SimpleNamespace(config=SimpleNamespace(chain=SimpleNamespace(netuid=118)))


async def _sweep_once(
    collector: SourceEmissionCollector, outcome: Exception | None
) -> None:
    """Run exactly one iteration of the real collector loop."""

    async def sweep() -> None:
        collector._stop.set()
        if outcome is not None:
            raise outcome

    collector._stop.clear()
    collector.sweep = sweep  # type: ignore[method-assign]
    await collector._run()


async def test_stalled_cursor_is_distinguishable_and_logged_once_per_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No cursor advance over many sweeps while blocked -> one ERROR, gauge 1.

    The sweep keeps failing (the finalized head moves, the collector cannot
    follow). The first failures are inside the threshold and stay quiet; once
    the cursor is older than the threshold the collector logs exactly one
    ERROR however many further sweeps fail, then a single recovery line when
    the cursor advances again.
    """
    now = datetime.now(UTC)
    cursor = SimpleNamespace(
        block=6_800_000,
        updated_at=now - timedelta(seconds=30),
        last_blocked_reason=None,
        runtime_code_hash="0xv468",
    )
    collector = SourceEmissionCollector(
        app_state=_APP_STATE,
        session_maker=lambda: _Session(cursor),
        interval_seconds=0.0,
    )
    caplog.set_level(logging.WARNING, logger=_LOGGER)

    await _sweep_once(collector, RuntimeError("unaudited runtime fingerprint"))
    assert cursor.last_blocked_reason == ("RuntimeError: unaudited runtime fingerprint")
    assert _stalled_gauge() == 0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    # The head kept moving for twenty minutes; the cursor did not.
    cursor.updated_at = now - timedelta(minutes=20)
    for _ in range(3):
        await _sweep_once(collector, RuntimeError("unaudited runtime fingerprint"))
    stalls = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and r.getMessage().startswith("source emission collector stalled")
    ]
    assert len(stalls) == 1
    record = stalls[0]
    assert record.source_emission_collector_stalled is True  # type: ignore[attr-defined]
    assert record.cursor_block == 6_800_000  # type: ignore[attr-defined]
    assert record.runtime_code_hash == "0xv468"  # type: ignore[attr-defined]
    assert record.cursor_lag_seconds >= 20 * 60  # type: ignore[attr-defined]
    assert "unaudited runtime fingerprint" in record.getMessage()
    assert _stalled_gauge() == 1
    assert (_lag_gauge() or 0) >= 20 * 60

    # The audited fingerprint ships; the next block commit advances the cursor
    # and clears the reason.
    cursor.updated_at = datetime.now(UTC)
    cursor.last_blocked_reason = None
    await _sweep_once(collector, None)
    recovered = [
        r
        for r in caplog.records
        if r.getMessage().startswith("source emission collector recovered")
    ]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.WARNING
    assert _stalled_gauge() == 0
    assert (_lag_gauge() or 0) < 60


async def test_staleness_read_failure_never_breaks_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Broken:
        async def __aenter__(self) -> _Broken:
            raise ConnectionError("database unavailable")

        async def __aexit__(self, *_args: Any) -> bool:
            return False

    collector = SourceEmissionCollector(
        app_state=_APP_STATE,
        session_maker=_Broken,
        interval_seconds=0.0,
    )
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    await _sweep_once(collector, None)
    assert any(
        r.getMessage() == "source emission collector staleness read failed"
        for r in caplog.records
    )
