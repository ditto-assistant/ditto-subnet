"""Append-only validator-slot settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import ValidatorSlotSettingsRevision, ValidatorTicket

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_validator_slot_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> ValidatorSlotSettingsRevision | None:
    return await session.scalar(
        select(ValidatorSlotSettingsRevision)
        .where(ValidatorSlotSettingsRevision.scope == scope)
        .order_by(ValidatorSlotSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_validator_slot_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[ValidatorSlotSettingsRevision]:
    return list(
        await session.scalars(
            select(ValidatorSlotSettingsRevision)
            .order_by(ValidatorSlotSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_validator_slot_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> ValidatorSlotSettingsRevision:
    """Append one revision. The ``flush`` is deliberate: it sends the INSERT now,
    so a racing writer that already took this ``(scope, parent_revision)``
    surfaces as an ``IntegrityError`` from THIS call (where the caller can turn
    it into a 409) rather than from a later commit."""
    row = ValidatorSlotSettingsRevision(
        parent_revision=parent_revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def live_lease_purpose_counts(
    session: AsyncSession, *, now: datetime
) -> dict[str, dict[str, int]]:
    """Live leases (``issued``, deadline ahead) per validator, by ticket purpose.

    The same liveness rule the slot cap and the round reservation count with,
    so the operator view of who holds what matches the issuance decision.
    """
    rows = (
        await session.execute(
            select(
                ValidatorTicket.validator_hotkey,
                ValidatorTicket.purpose,
                func.count(),
            )
            .where(
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.deadline > now,
            )
            .group_by(ValidatorTicket.validator_hotkey, ValidatorTicket.purpose)
        )
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for hotkey, purpose, count in rows:
        counts.setdefault(hotkey, {})[str(purpose)] = int(count)
    return counts
