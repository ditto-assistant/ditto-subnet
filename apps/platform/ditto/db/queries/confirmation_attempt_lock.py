"""Canonical pessimistic lock order for one Bench v9 confirmation attempt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from ditto.db.models import (
    ConfirmationBudgetDay,
    ConfirmationBudgetReservation,
    ConfirmationBundle,
    ConfirmationBundleTicket,
)
from ditto.db.queries.confirmation_bundles import ConfirmationBundlePersistenceError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class LockedConfirmationAttempt:
    """One confirmation attempt locked in the global lifecycle order."""

    ticket: ConfirmationBundleTicket
    reservation: ConfirmationBudgetReservation
    budget: ConfirmationBudgetDay
    bundle: ConfirmationBundle


async def lock_confirmation_attempt(
    session: AsyncSession,
    *,
    bundle_id: UUID,
    ticket_id: UUID,
) -> LockedConfirmationAttempt | None:
    """Lock ticket -> reservation -> budget day -> bundle, or report no ticket.

    Claim resume, recovery, prepare, fail, and submit all touch these rows. One
    shared helper prevents a caller from holding the bundle while another owns
    the ticket or budget row for the same attempt.
    """
    ticket = await session.get(
        ConfirmationBundleTicket, ticket_id, with_for_update=True
    )
    if ticket is None or ticket.bundle_id != bundle_id:
        return None
    reservation = await session.scalar(
        select(ConfirmationBudgetReservation)
        .where(
            ConfirmationBudgetReservation.bundle_id == bundle_id,
            ConfirmationBudgetReservation.attempt == ticket.attempt,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reservation is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation attempt has no budget reservation"
        )
    budget = await session.get(
        ConfirmationBudgetDay,
        reservation.utc_day,
        with_for_update=True,
        populate_existing=True,
    )
    if budget is None:
        raise ConfirmationBundlePersistenceError(
            "confirmation attempt budget day disappeared"
        )
    bundle = await session.get(ConfirmationBundle, bundle_id, with_for_update=True)
    if bundle is None:  # pragma: no cover - protected by ticket foreign key
        raise ConfirmationBundlePersistenceError("confirmation bundle disappeared")
    return LockedConfirmationAttempt(
        ticket=ticket,
        reservation=reservation,
        budget=budget,
        bundle=bundle,
    )


__all__ = ["LockedConfirmationAttempt", "lock_confirmation_attempt"]
