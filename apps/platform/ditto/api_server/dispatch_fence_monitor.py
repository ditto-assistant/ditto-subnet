"""What the validator job allocator is busy with, for Backroom.

``POST /validator/job`` takes one non-blocking advisory lock (the dispatch
fence, :mod:`ditto.db.queries.rollout_dispatch`) for its whole transaction.
A poll that finds it held is answered 204 ``allocator_busy``. The outcome
tally says *how often* that happens; this module says *why*: who holds the
fence, for how long each hold lasts, how much of the window the fence is held
at all, and whether the polls that bounce off it collide with another
validator or with their own host's sibling slots.

That split is the diagnosis. A fence held most of the window means the
allocator itself is slow. A fence held briefly while one host's slots keep
bouncing off each other means that host polls in synchronized bursts.

Observational only, like the outcome tally: Platform API runs as one pm2 fork
process, so a process-local monitor sees every poll, and a deploy resets it.
Nothing here feeds dispatch, and every record path swallows its own errors.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

from ditto.metrics import (
    VALIDATOR_DISPATCH_FENCE_BUSY,
    VALIDATOR_DISPATCH_FENCE_HOLD_SECONDS,
)

logger = logging.getLogger(__name__)

RECENT_WINDOW = timedelta(minutes=15)
# Bounds memory under a poll storm: at 2.6 polls/s a 15-minute window is about
# 2,300 events, so this keeps the whole window in the ordinary storm and the
# most recent slice of anything worse.
MAX_EVENTS = 8192
TOP_N = 10


@dataclass(frozen=True)
class _Hold:
    ended_at: datetime
    seconds: float
    validator_hotkey: str
    slot_id: str
    outcome: str


@dataclass(frozen=True)
class _Busy:
    at: datetime
    validator_hotkey: str
    slot_id: str
    holder_hotkey: str | None


@dataclass
class _Holder:
    validator_hotkey: str
    slot_id: str
    acquired_at: datetime
    started: float


class DispatchFenceMonitor:
    """Process-local record of dispatch-fence holds and busy declines."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self.tracking_since = self._clock()
        self._holder: _Holder | None = None
        self._holds: deque[_Hold] = deque(maxlen=MAX_EVENTS)
        self._busy: deque[_Busy] = deque(maxlen=MAX_EVENTS)

    def attempt(self, validator_hotkey: str, slot_id: str) -> FenceAttempt:
        """Scope one job poll's fence attempt; see :class:`FenceAttempt`."""
        return FenceAttempt(self, validator_hotkey, slot_id)

    def _acquired(self, validator_hotkey: str, slot_id: str) -> _Holder:
        holder = _Holder(
            validator_hotkey=validator_hotkey,
            slot_id=slot_id,
            acquired_at=self._clock(),
            started=self._monotonic(),
        )
        self._holder = holder
        return holder

    def _released(self, holder: _Holder, outcome: str) -> None:
        seconds = max(0.0, self._monotonic() - holder.started)
        if self._holder is holder:
            self._holder = None
        VALIDATOR_DISPATCH_FENCE_HOLD_SECONDS.observe(seconds)
        self._holds.append(
            _Hold(
                ended_at=self._clock(),
                seconds=seconds,
                validator_hotkey=holder.validator_hotkey,
                slot_id=holder.slot_id,
                outcome=outcome,
            )
        )

    def _busy_decline(
        self, validator_hotkey: str, slot_id: str, observed: _Holder | None
    ) -> None:
        # The lock check is a database round trip: the holder seen just before
        # it is the one that most likely refused this poll. Fall back to the
        # holder at record time only when none was tracked before the check
        # (it acquired during the round trip). Best-effort: a holder that
        # released and was replaced within one round trip is still misnamed.
        holder = observed if observed is not None else self._holder
        holder_hotkey = holder.validator_hotkey if holder is not None else None
        if holder_hotkey is None:
            collision = "untracked"
        elif holder_hotkey == validator_hotkey:
            collision = "same_validator"
        else:
            collision = "other_validator"
        VALIDATOR_DISPATCH_FENCE_BUSY.labels(collision=collision).inc()
        self._busy.append(
            _Busy(
                at=self._clock(),
                validator_hotkey=validator_hotkey,
                slot_id=slot_id,
                holder_hotkey=holder_hotkey,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        cutoff = now - RECENT_WINDOW
        holds = [hold for hold in self._holds if hold.ended_at >= cutoff]
        busy = [event for event in self._busy if event.at >= cutoff]
        window_seconds = min(
            RECENT_WINDOW.total_seconds(),
            max((now - self.tracking_since).total_seconds(), 1e-9),
        )
        durations = sorted(hold.seconds for hold in holds)
        # Utilization counts only the part of each hold inside the window,
        # including the hold still in progress; the quantiles above keep each
        # hold's full duration.
        held_seconds = sum(
            max(
                0.0,
                (
                    hold.ended_at
                    - max(hold.ended_at - timedelta(seconds=hold.seconds), cutoff)
                ).total_seconds(),
            )
            for hold in holds
        )
        live = self._holder
        if live is not None:
            # Elapsed time comes from the monotonic clock: a wall-clock jump
            # during a live hold must not shrink or inflate it.
            held_seconds += min(
                max(0.0, self._monotonic() - live.started),
                RECENT_WINDOW.total_seconds(),
            )

        by_validator: dict[str, list[float]] = {}
        for hold in holds:
            by_validator.setdefault(hold.validator_hotkey, []).append(hold.seconds)
        hold_rows: list[dict[str, Any]] = [
            {
                "validator_hotkey": hotkey,
                "holds": len(values),
                "held_seconds": round(sum(values), 3),
                "max_hold_seconds": round(max(values), 3),
            }
            for hotkey, values in sorted(
                by_validator.items(), key=lambda item: (-sum(item[1]), item[0])
            )[:TOP_N]
        ]

        busy_by_pair: Counter[tuple[str, str | None]] = Counter(
            (event.validator_hotkey, event.holder_hotkey) for event in busy
        )
        busy_by_slot: dict[str, Counter[str]] = {}
        for event in busy:
            busy_by_slot.setdefault(event.validator_hotkey, Counter())[
                event.slot_id
            ] += 1
        busy_rows = [
            {
                "validator_hotkey": blocked,
                "holder_hotkey": holder,
                "busy_declines": count,
                "same_validator": holder == blocked,
                "by_slot": dict(sorted(busy_by_slot[blocked].items())),
            }
            for (blocked, holder), count in sorted(
                busy_by_pair.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1] or ""),
            )[:TOP_N]
        ]
        outcomes = Counter(hold.outcome for hold in holds)
        holder = self._holder
        return {
            "observed_at": now,
            "tracking_since": self.tracking_since,
            "recent_window_seconds": int(RECENT_WINDOW.total_seconds()),
            "holder": (
                {
                    "validator_hotkey": holder.validator_hotkey,
                    "slot_id": holder.slot_id,
                    "acquired_at": holder.acquired_at,
                    "held_seconds": round(
                        max(0.0, self._monotonic() - holder.started), 3
                    ),
                }
                if holder is not None
                else None
            ),
            "recent_holds": len(durations),
            "recent_held_seconds": round(held_seconds, 3),
            "recent_utilization": round(min(1.0, held_seconds / window_seconds), 4),
            "hold_p50_seconds": _quantile(durations, 0.50),
            "hold_p95_seconds": _quantile(durations, 0.95),
            "hold_max_seconds": round(durations[-1], 3) if durations else None,
            "hold_outcomes": dict(sorted(outcomes.items())),
            "recent_busy_declines": len(busy),
            "recent_busy_same_validator": sum(
                event.holder_hotkey == event.validator_hotkey for event in busy
            ),
            "recent_busy_untracked_holder": sum(
                event.holder_hotkey is None for event in busy
            ),
            "holds_by_validator": hold_rows,
            "busy_by_validator": busy_rows,
        }


def _quantile(sorted_values: list[float], q: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return round(sorted_values[index], 3)


class FenceAttempt:
    """Async context for one ``/validator/job`` transaction's fence.

    Enter it *outside* ``session.begin()`` (``async with attempt,
    session.begin():``) so it exits after the commit: the advisory lock is
    transaction-scoped and is released by that commit, not before. Inside,
    call :meth:`before_lock_check` just before trying the lock, then
    :meth:`acquired` once it is taken or :meth:`busy` when it is not.
    """

    def __init__(
        self, monitor: DispatchFenceMonitor, validator_hotkey: str, slot_id: str
    ) -> None:
        self._monitor = monitor
        self._validator_hotkey = validator_hotkey
        self._slot_id = slot_id
        self._holder: _Holder | None = None
        self._observed_holder: _Holder | None = None
        self._outcome: Callable[[], str | None] = lambda: None

    def before_lock_check(self) -> None:
        """Remember who held the fence just before this poll tries to take it."""
        self._observed_holder = self._monitor._holder

    def acquired(self, outcome: Callable[[], str | None] | None = None) -> None:
        """Mark the fence taken; ``outcome`` labels the hold when it ends."""
        try:
            self._holder = self._monitor._acquired(
                self._validator_hotkey, self._slot_id
            )
            if outcome is not None:
                self._outcome = outcome
        except Exception:  # pragma: no cover - observability must not fail a poll
            logger.exception("dispatch fence monitor failed to record a hold")

    def busy(self) -> None:
        """Record a poll bounced off a held fence, naming who held it."""
        try:
            self._monitor._busy_decline(
                self._validator_hotkey, self._slot_id, self._observed_holder
            )
        except Exception:  # pragma: no cover - observability must not fail a poll
            logger.exception("dispatch fence monitor failed to record busy")

    async def __aenter__(self) -> FenceAttempt:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        holder = self._holder
        if holder is None:
            return
        self._holder = None
        outcome = f"error_{exc_type.__name__}" if exc_type is not None else None
        if outcome is None:
            # A failing label must not strand the hold as the live holder.
            try:
                outcome = self._outcome() or "completed"
            except Exception:
                logger.exception("dispatch fence outcome label failed")
                outcome = "unlabelled"
        try:
            self._monitor._released(holder, outcome)
        except Exception:  # pragma: no cover - observability must not fail a poll
            logger.exception("dispatch fence monitor failed to close a hold")


DISPATCH_FENCE = DispatchFenceMonitor()

__all__ = ["DISPATCH_FENCE", "DispatchFenceMonitor", "FenceAttempt"]
