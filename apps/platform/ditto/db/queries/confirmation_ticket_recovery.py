"""Crash recovery for overdue private Bench v9 confirmation leases."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import ConfirmationBundleTicket
from ditto.db.queries.confirmation_attempt_lock import lock_confirmation_attempt
from ditto.db.queries.confirmation_bundles import (
    ConfirmationBundlePersistenceError,
    settle_confirmation_bundle_budget,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def expire_overdue_confirmation_bundle_tickets(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = 100,
) -> int:
    """Pessimistically settle bounded overdue work exactly once.

    A crashed validator cannot report partial provider spend. Charging the
    reservation ceiling is the only fail-closed accounting choice: it releases
    outstanding liability without understating cost, closes the ticket, and
    leaves the bundle retryable. ``SKIP LOCKED`` makes this safe against both a
    concurrent cooperative failure and another claim-side recovery sweep.
    """
    if now.tzinfo is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation recovery time must be timezone-aware"
        )
    if not 1 <= limit <= 1_000:
        raise ValueError("confirmation recovery limit must be in [1, 1000]")
    tickets = list(
        await session.scalars(
            select(ConfirmationBundleTicket)
            .where(
                ConfirmationBundleTicket.status == "issued",
                ConfirmationBundleTicket.deadline <= now,
            )
            .order_by(
                ConfirmationBundleTicket.deadline,
                ConfirmationBundleTicket.ticket_id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    settled = 0
    for ticket in tickets:
        attempt = await lock_confirmation_attempt(
            session,
            bundle_id=ticket.bundle_id,
            ticket_id=ticket.ticket_id,
        )
        if attempt is None:  # pragma: no cover - selected ticket is authoritative
            raise ConfirmationBundlePersistenceError(
                "overdue confirmation ticket disappeared"
            )
        result = await settle_confirmation_bundle_budget(
            session,
            reservation_id=attempt.reservation.reservation_id,
            expected_revision=attempt.budget.revision,
            actual_microusd=attempt.reservation.reserved_microusd,
            failed_attempt=True,
            settled_at=now,
        )
        if not result.replayed:
            settled += 1
    return settled


__all__ = ["expire_overdue_confirmation_bundle_tickets"]
