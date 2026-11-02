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
opened in the window when its first continual-retest lease was issued at or
after that instant.

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
from ditto.db.models import LedgerEpochSnapshot, ValidatorTicket

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

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
    session: AsyncSession, *, cadence_tempos: int, seeds_per_window: int
) -> WaveWindow:
    """Resolve the window the chain is in now from the ledger pins."""
    if cadence_tempos <= 0:
        return WaveWindow(cadence_tempos, seeds_per_window, None, None, None)
    latest = await session.scalar(select(func.max(LedgerEpochSnapshot.epoch_index)))
    if latest is None:
        return WaveWindow(cadence_tempos, seeds_per_window, None, None, None)
    window_epoch = int(latest) - int(latest) % cadence_tempos
    window_start = await session.scalar(
        select(func.min(LedgerEpochSnapshot.pinned_at)).where(
            LedgerEpochSnapshot.epoch_index >= window_epoch
        )
    )
    return WaveWindow(
        cadence_tempos, seeds_per_window, int(latest), window_epoch, window_start
    )


async def first_retest_issue_by_seed(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    bench_version: int,
) -> dict[int, datetime]:
    """When each retest seed was first leased to any of these agents.

    Every seed the lane ever leased them, whichever reign introduced it: a
    catch-up or void re-run lease of an old seed keeps that seed's original
    first-issue time, so only genuinely fresh seeds land inside a window.
    """
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    rows = await session.execute(
        select(ValidatorTicket.seed, func.min(ValidatorTicket.issued_at))
        .where(
            ValidatorTicket.agent_id.in_(ids),
            ValidatorTicket.bench_version == bench_version,
            ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
            ValidatorTicket.seed.is_not(None),
        )
        .group_by(ValidatorTicket.seed)
    )
    return {int(seed): issued for seed, issued in rows if seed is not None}


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
