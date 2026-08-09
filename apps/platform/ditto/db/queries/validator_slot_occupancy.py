"""Cross-table serialization for one validator execution slot.

Ordinary and v9 confirmation tickets intentionally use separate tables, so a
database unique constraint cannot span them.  Every allocator must take this
same transaction-scoped advisory lock before checking either table.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import ConfirmationBundleTicket, ValidatorTicket

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_validator_slot(
    session: AsyncSession, *, validator_hotkey: str, slot_id: str
) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"{validator_hotkey}:{slot_id}", 0)
                )
            )
        )


async def live_ordinary_slot_ticket(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
) -> ValidatorTicket | None:
    return await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.slot_id == slot_id,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.deadline > now,
        )
        .order_by(ValidatorTicket.issued_at, ValidatorTicket.agent_id)
        .limit(1)
        .with_for_update()
    )


async def live_v9_confirmation_slot_ticket(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    slot_id: str,
    now: datetime,
) -> ConfirmationBundleTicket | None:
    return await session.scalar(
        select(ConfirmationBundleTicket)
        .where(
            ConfirmationBundleTicket.validator_hotkey == validator_hotkey,
            ConfirmationBundleTicket.slot_id == slot_id,
            ConfirmationBundleTicket.status == "issued",
            ConfirmationBundleTicket.deadline > now,
        )
        .order_by(
            ConfirmationBundleTicket.issued_at,
            ConfirmationBundleTicket.ticket_id,
        )
        .limit(1)
        .with_for_update()
    )


__all__ = [
    "live_ordinary_slot_ticket",
    "live_v9_confirmation_slot_ticket",
    "lock_validator_slot",
]
