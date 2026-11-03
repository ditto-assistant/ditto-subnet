"""Tempo-aligned wave pacing for continual retests (``wave_cadence_tempos``).

The continual lane grows a reign's shared seed family one seed at a time: the
next seed opens as soon as every emission-set member holds the previous one.
That pacing follows validator throughput, so the board moves whenever the fleet
happens to finish a wave. With a positive cadence ``N`` the lane is aligned to
the chain instead:

* the ``SubnetEpochIndex`` grid is cut into windows of ``N`` epochs starting at
  a multiple of ``N`` (the same grid generator rounds use at
  ``epochs_per_round`` ``N``);
* a window may OPEN at most ``wave_seeds_per_window`` new shared seeds;
* a seed already opened keeps being leased until the wave completes, and
  catch-up of recorded coverage is never paced.

A window starts when the first ledger pin of its first epoch was taken
(:class:`~ditto.db.models.LedgerEpochSnapshot`, one per chain epoch), so the
boundary is a chain fact rather than a wall-clock guess. A seed counts as
opened in the window when it first entered the lane (its first lease or
accepted score, lane-wide) at or after that instant.

Pacing fails open: with no ledger pin to anchor a window (``ledger_pin_mode``
``live`` or a fresh database) the lane keeps its historical pacing, because the
gate only ever withholds work and must never strand an open wave.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ditto.api_models.ticket_status import TicketPurpose
from ditto.db.models import ConfirmationScore, LedgerEpochSnapshot, ValidatorTicket

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class WaveWindow:
    """The current tempo-aligned window, or the reason there is none."""

    cadence_tempos: int
    seeds_per_window: int
    latest_epoch: int | None
    window_epoch: int | None
    """First ``SubnetEpochIndex`` of the current window."""
    window_start: datetime | None
    """When that epoch's first ledger pin was taken; null means fail open."""

    @property
    def paced(self) -> bool:
        return self.cadence_tempos > 0 and self.window_start is not None

    @property
    def next_window_epoch(self) -> int | None:
        if self.window_epoch is None or self.cadence_tempos <= 0:
            return None
        return self.window_epoch + self.cadence_tempos


async def current_wave_window(
    session: AsyncSession,
    *,
    cadence_tempos: int,
    seeds_per_window: int,
    netuid: int | None = None,
) -> WaveWindow:
    """Resolve the window the chain is in now from this subnet's ledger pins."""
    if cadence_tempos <= 0:
        return WaveWindow(cadence_tempos, seeds_per_window, None, None, None)
    scope = (LedgerEpochSnapshot.netuid == netuid,) if netuid is not None else ()
    latest = await session.scalar(
        select(func.max(LedgerEpochSnapshot.epoch_index)).where(*scope)
    )
    if latest is None:
        return WaveWindow(cadence_tempos, seeds_per_window, None, None, None)
    window_epoch = int(latest) - int(latest) % cadence_tempos
    window_start = await session.scalar(
        select(func.min(LedgerEpochSnapshot.pinned_at)).where(
            *scope, LedgerEpochSnapshot.epoch_index >= window_epoch
        )
    )
    return WaveWindow(
        cadence_tempos, seeds_per_window, int(latest), window_epoch, window_start
    )


async def first_retest_issue_by_seed(
    session: AsyncSession,
    *,
    bench_version: int,
    since: datetime,
    include: Iterable[int] = (),
) -> dict[int, datetime]:
    """When each candidate seed first entered the continual lane at this version.

    Candidates are the seeds with lane activity at or after ``since`` (the
    window start) plus ``include`` (the seed about to be opened), so the read is
    bounded by one window's activity, not the lane's whole history. For each
    candidate the first-seen time is lane-wide and durable:

    * lane-wide, never scoped to the current cohort, because a seed opened for
      a member that later left the emission set still spent its window budget;
    * durable, because ``ValidatorTicket`` rows are reused per (agent, version,
      validator) and keep only their latest lease. The append-only
      ``ConfirmationScore`` row (voided rows included: a void does not un-open
      a seed) is the record, and a live lease covers a seed not yet scored.

    A seed whose only lease was overwritten before any score landed reads as
    unopened, which can only let pacing open one more seed, never strand a wave.
    """
    score_scope = (ConfirmationScore.bench_version == bench_version,)
    lease_scope = (
        ValidatorTicket.bench_version == bench_version,
        ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
        ValidatorTicket.seed.is_not(None),
    )
    candidates = {int(seed) for seed in include}
    recent_scores = await session.scalars(
        select(ConfirmationScore.seed)
        .where(*score_scope, ConfirmationScore.created_at >= since)
        .distinct()
    )
    candidates.update(int(seed) for seed in recent_scores if seed is not None)
    recent_leases = await session.scalars(
        select(ValidatorTicket.seed)
        .where(*lease_scope, ValidatorTicket.issued_at >= since)
        .distinct()
    )
    candidates.update(int(seed) for seed in recent_leases if seed is not None)
    if not candidates:
        return {}
    wanted = sorted(candidates)
    first: dict[int, datetime] = {}
    scored = await session.execute(
        select(ConfirmationScore.seed, func.min(ConfirmationScore.created_at))
        .where(*score_scope, ConfirmationScore.seed.in_(wanted))
        .group_by(ConfirmationScore.seed)
    )
    for seed, created in scored:
        if seed is not None and created is not None:
            first[int(seed)] = created
    leased = await session.execute(
        select(ValidatorTicket.seed, func.min(ValidatorTicket.issued_at))
        .where(*lease_scope, ValidatorTicket.seed.in_(wanted))
        .group_by(ValidatorTicket.seed)
    )
    for seed, issued in leased:
        if seed is None or issued is None:
            continue
        key = int(seed)
        if key not in first or _at_or_after(first[key], issued):
            first[key] = issued
    return first


def seeds_opened_in_window(
    window: WaveWindow, first_issue: dict[int, datetime]
) -> frozenset[int]:
    """Seeds whose first lease falls inside the current window."""
    if window.window_start is None:
        return frozenset()
    start = window.window_start
    return frozenset(
        seed for seed, issued in first_issue.items() if _at_or_after(issued, start)
    )


def growth_open(
    window: WaveWindow, *, next_seed: int, first_issue: dict[int, datetime]
) -> bool:
    """Whether the lane may lease ``next_seed`` as wave growth now.

    An already-opened seed is always open (its wave must be able to finish);
    a fresh seed opens only while the window has budget left.
    """
    if not window.paced or next_seed in first_issue:
        return True
    return len(seeds_opened_in_window(window, first_issue)) < window.seeds_per_window


def _at_or_after(value: datetime, start: datetime) -> bool:
    # SQLite test databases return naive datetimes for timezone columns.
    if (value.tzinfo is None) != (start.tzinfo is None):
        value = value.replace(tzinfo=None)
        start = start.replace(tzinfo=None)
    return value >= start
