"""Bounty deadlines: one rule, applied lazily and by a sweeper.

Every bounty write applies the bounty's due deadlines first, under its bounty
lock, and a platform-role sweeper applies them to bounties nobody touches.
Both run :func:`apply_deadlines`, which stamps each transition at its
deadline (never when it was noticed) and is idempotent, so a restart, a race
or a second replica changes nothing:

- an unpaid hold past ``hold_expires_at`` reopens its slot (``hold_expired``);
- a claimed slot past its delivery deadline, or a rework slot past its rework
  deadline, expires (``deadline_missed``); the entry fee is retained;
- a submission past its review due date stays held, since the contributor is
  not at fault, and is reported overdue once per due date (``review_overdue``).

Reads never write: :func:`effective_slot_status` shows the same outcome
before any request or sweep records it. A deadline is reached at its instant:
the slot changes at ``now >= deadline``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, exists, or_, select

from ditto.db.models import Bounty, BountyClaim, BountyEvent, BountySlot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0
# Slots whose status a delivery or rework deadline ends.
DEADLINE_STATUSES = ("claimed", "rework")


def effective_slot_status(
    slot: BountySlot, holder: BountyClaim | None, *, now: datetime
) -> str:
    """A slot's status as of ``now``, before any write records it."""
    if slot.status == "held" and (holder is None or now >= holder.hold_expires_at):
        return "open"
    if (
        slot.status in DEADLINE_STATUSES
        and slot.deadline_at is not None
        and now >= slot.deadline_at
    ):
        return "expired"
    return slot.status


def review_overdue(slot: BountySlot, *, now: datetime) -> bool:
    return (
        slot.status == "submitted"
        and slot.review_due_at is not None
        and now >= slot.review_due_at
    )


def release_slot(
    session: AsyncSession,
    slot: BountySlot,
    claim: BountyClaim,
    *,
    kind: str,
    at: datetime,
) -> None:
    """Return a held slot to open, with its public event."""
    slot.status, slot.claim_id, slot.status_changed_at = "open", None, at
    session.add(
        BountyEvent(
            bounty_id=slot.bounty_id,
            kind=kind,
            revision=claim.revision,
            round=slot.round,
            slot_id=slot.slot_id,
            claim_id=claim.claim_id,
            recorded_at=at,
        )
    )


def _overdue_reported():
    return exists().where(
        BountyEvent.slot_id == BountySlot.slot_id,
        BountyEvent.kind == "review_overdue",
        BountyEvent.recorded_at == BountySlot.review_due_at,
    )


async def apply_deadlines(
    session: AsyncSession, bounty_id: UUID, *, now: datetime
) -> None:
    """Record every deadline of one bounty that ``now`` has reached.

    The caller holds the bounty's row lock.
    """
    rows = (
        await session.execute(
            select(BountySlot, BountyClaim)
            .join(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
            .where(
                BountySlot.bounty_id == bounty_id,
                or_(
                    and_(
                        BountySlot.status == "held",
                        BountyClaim.hold_expires_at <= now,
                    ),
                    and_(
                        BountySlot.status.in_(DEADLINE_STATUSES),
                        BountySlot.deadline_at <= now,
                    ),
                    and_(
                        BountySlot.status == "submitted",
                        BountySlot.review_due_at <= now,
                        ~_overdue_reported(),
                    ),
                ),
            )
            .order_by(BountySlot.round, BountySlot.position)
            .with_for_update(of=BountySlot)
        )
    ).all()
    for slot, claim in rows:
        if slot.status == "held":
            release_slot(
                session, slot, claim, kind="hold_expired", at=claim.hold_expires_at
            )
            continue
        if slot.status in DEADLINE_STATUSES:
            assert slot.deadline_at is not None
            at = slot.deadline_at
            slot.status, slot.status_changed_at = "expired", at
            kind = "deadline_missed"
        else:
            assert slot.review_due_at is not None
            at, kind = slot.review_due_at, "review_overdue"
        session.add(
            BountyEvent(
                bounty_id=bounty_id,
                kind=kind,
                revision=claim.revision,
                round=slot.round,
                slot_id=slot.slot_id,
                claim_id=claim.claim_id,
                recorded_at=at,
            )
        )
    await session.flush()


async def settle_deadlines(
    maker: async_sessionmaker[AsyncSession], bounty_id: UUID, *, now: datetime
) -> None:
    """Record a bounty's due deadlines in their own committed transaction.

    Deadlines are facts, whatever happens to the request that noticed them:
    a refused action must not roll back the expiry it ran into. Actions settle
    first, then apply again under their own lock, which is idempotent.
    """
    async with maker() as session, session.begin():
        locked = await session.scalar(
            select(Bounty.bounty_id)
            .where(Bounty.bounty_id == bounty_id)
            .with_for_update()
        )
        if locked is not None:
            await apply_deadlines(session, bounty_id, now=now)


async def due_bounties(session: AsyncSession, *, now: datetime) -> list[UUID]:
    """Bounties with at least one deadline ``now`` has reached and not recorded."""
    return list(
        await session.scalars(
            select(BountySlot.bounty_id)
            .outerjoin(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
            .where(
                or_(
                    and_(
                        BountySlot.status == "held",
                        BountyClaim.hold_expires_at <= now,
                    ),
                    and_(
                        BountySlot.status.in_(DEADLINE_STATUSES),
                        BountySlot.deadline_at <= now,
                    ),
                    and_(
                        BountySlot.status == "submitted",
                        BountySlot.review_due_at <= now,
                        ~_overdue_reported(),
                    ),
                )
            )
            .distinct()
        )
    )


class BountyDeadlineSweeper:
    """Platform-role loop recording deadlines nobody's request has touched."""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("bounty deadline sweep interval must be positive")
        self._session_maker = session_maker
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            # A fresh stop signal, so a closed sweeper can start again.
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(
                self._run(), name="bounty-deadline-sweeper"
            )

    async def aclose(self, *, grace_seconds: float = 5.0) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_seconds)
        except TimeoutError:
            # A sweep still blocked (say, on a row lock) is cut short: its
            # transaction rolls back and the next start settles the same
            # deadlines, so shutdown never waits on it.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def sweep(self, *, now: datetime | None = None) -> int:
        """Apply due deadlines, one bounty per transaction; returns the count.

        One bounty's failure is logged and never blocks the rest of the sweep.
        """
        at = now or datetime.now(UTC)
        async with self._session_maker() as session:
            bounty_ids = await due_bounties(session, now=at)
        settled = 0
        for bounty_id in bounty_ids:
            # Closing stops between bounties; the rest wait for the next start.
            if self._stop.is_set():
                break
            try:
                await settle_deadlines(self._session_maker, bounty_id, now=at)
            except Exception:
                logger.exception("bounty %s deadlines failed to settle", bounty_id)
                continue
            settled += 1
        return settled

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
            except Exception:
                # Requests apply the same deadlines lazily, so a failed sweep
                # only delays when an untouched bounty's trail records them.
                logger.exception("bounty deadline sweep failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue
