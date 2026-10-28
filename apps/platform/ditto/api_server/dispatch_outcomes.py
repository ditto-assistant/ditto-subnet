"""Per-validator tally of how ``/validator/job`` polls end, for Backroom.

``ditto_validator_dispatch_declined_total`` deliberately carries no hotkey,
so "why is *this* validator idle?" used to mean grepping the API log for
``declined job reason=`` lines or reading the prod DB. This module keeps a
small in-process tally per (validator, lane, outcome) instead: totals since
the process started, a rolling 15-minute count, the last time each outcome
fired, and for 4xx/5xx the last error detail.

It is observational only. Nothing here feeds dispatch, and a crash in it must
never fail a poll, so every record path is wrapped. Platform API runs as one
pm2 fork process, so one process-local tally sees every poll; a deploy resets
it, which ``tracking_since`` reports.

Only authenticated hotkeys get a row. A poll that failed authentication goes
to one global counter, so an unauthenticated caller cannot evict real rows by
inventing hotkeys.
"""

from __future__ import annotations

import contextvars
import functools
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, ParamSpec, TypeVar

from fastapi import HTTPException, Response

logger = logging.getLogger(__name__)

DispatchLane = Literal["job", "top5_confirmation"]
RECENT_WINDOW = timedelta(minutes=15)
MAX_VALIDATORS = 256
MAX_DETAIL_CHARS = 200

_decline_reason: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dispatch_decline_reason", default=None
)


def note_decline_reason(reason: str) -> None:
    """Remember why the poll being handled is about to return 204."""
    _decline_reason.set(reason)


@dataclass
class OutcomeStats:
    total: int = 0
    first_at: datetime | None = None
    last_at: datetime | None = None
    last_detail: str | None = None
    last_slot_id: str | None = None
    recent: deque[datetime] = field(default_factory=deque)

    def add(self, now: datetime, detail: str | None, slot_id: str | None) -> None:
        self.total += 1
        self.first_at = self.first_at or now
        self.last_at = now
        if detail is not None:
            self.last_detail = detail[:MAX_DETAIL_CHARS]
        if slot_id is not None:
            self.last_slot_id = slot_id
        self.recent.append(now)
        self.prune(now)

    def prune(self, now: datetime) -> int:
        cutoff = now - RECENT_WINDOW
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        return len(self.recent)


class DispatchOutcomeTally:
    """Bounded per-(validator, lane, outcome) counters held in this process."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self.tracking_since = self._clock()
        self.unauthenticated: Counter[str] = Counter()
        self._rows: dict[tuple[str, str], dict[str, OutcomeStats]] = {}
        self._touched: dict[tuple[str, str], datetime] = {}

    def record(
        self,
        validator_hotkey: str,
        lane: str,
        outcome: str,
        *,
        detail: str | None = None,
        slot_id: str | None = None,
    ) -> None:
        now = self._clock()
        key = (validator_hotkey, lane)
        if key not in self._rows and len(self._rows) >= MAX_VALIDATORS:
            oldest = min(self._touched, key=self._touched.__getitem__)
            del self._rows[oldest]
            del self._touched[oldest]
        stats = self._rows.setdefault(key, {}).setdefault(outcome, OutcomeStats())
        stats.add(now, detail, slot_id)
        self._touched[key] = now

    def snapshot(self, validator_hotkey: str | None = None) -> dict[str, Any]:
        now = self._clock()
        rows = []
        for (hotkey, lane), outcomes in sorted(self._rows.items()):
            if validator_hotkey is not None and hotkey != validator_hotkey:
                continue
            entries = [
                {
                    "outcome": outcome,
                    "total": stats.total,
                    "recent_15m": stats.prune(now),
                    "first_at": stats.first_at,
                    "last_at": stats.last_at,
                    "last_detail": stats.last_detail,
                    "last_slot_id": stats.last_slot_id,
                }
                for outcome, stats in sorted(
                    outcomes.items(),
                    key=lambda item: item[1].last_at or self.tracking_since,
                    reverse=True,
                )
            ]
            last_at = entries[0]["last_at"] if entries else None
            rows.append(
                {
                    "validator_hotkey": hotkey,
                    "lane": lane,
                    "last_poll_at": last_at,
                    "last_outcome": entries[0]["outcome"] if entries else None,
                    "outcomes": entries,
                }
            )
        return {
            "observed_at": now,
            "tracking_since": self.tracking_since,
            "recent_window_seconds": int(RECENT_WINDOW.total_seconds()),
            "unauthenticated": dict(self.unauthenticated),
            "validators": rows,
        }


DISPATCH_OUTCOMES = DispatchOutcomeTally()

P = ParamSpec("P")
R = TypeVar("R")


def _outcome_from_exception(exc: BaseException) -> tuple[str, str | None]:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else repr(exc.detail)
        return f"http_{exc.status_code}", detail
    return f"error_{type(exc).__name__}", str(exc) or None


def track_dispatch_outcomes(
    lane: DispatchLane,
    *,
    unauthenticated: tuple[type[BaseException], ...] = (),
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Record how each poll of one dispatch endpoint ends.

    ``functools.wraps`` keeps the endpoint's signature visible to FastAPI.
    The endpoint's ``payload`` keyword supplies the signed hotkey and slot.
    """

    def decorate(endpoint: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(endpoint)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            token = _decline_reason.set(None)
            payload = kwargs.get("payload")
            try:
                result = await endpoint(*args, **kwargs)
            except unauthenticated as exc:
                _safely(_record_unauthenticated, lane, type(exc).__name__)
                raise
            except BaseException as exc:
                outcome, detail = _outcome_from_exception(exc)
                _safely(_record, payload, lane, outcome, detail)
                raise
            else:
                status = result.status_code if isinstance(result, Response) else 200
                if status == 204:
                    outcome = _decline_reason.get() or "declined_unattributed"
                elif status < 300:
                    outcome = "issued"
                else:
                    outcome = f"http_{status}"
                _safely(_record, payload, lane, outcome, None)
                return result
            finally:
                _decline_reason.reset(token)

        return wrapper

    return decorate


def _record(payload: Any, lane: str, outcome: str, detail: str | None) -> None:
    hotkey = getattr(payload, "validator_hotkey", None)
    if not isinstance(hotkey, str):
        return
    slot_id = getattr(payload, "slot_id", None)
    DISPATCH_OUTCOMES.record(
        hotkey,
        lane,
        outcome,
        detail=detail,
        slot_id=slot_id if isinstance(slot_id, str) else None,
    )


def _record_unauthenticated(lane: str, error: str) -> None:
    DISPATCH_OUTCOMES.unauthenticated[f"{lane}:{error}"] += 1


def _safely(record: Callable[..., None], *args: Any) -> None:
    try:
        record(*args)
    except Exception:  # pragma: no cover - observability must not fail a poll
        logger.exception("dispatch outcome tally failed")
