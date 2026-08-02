"""Append-only validator-slot settings reads and writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import ValidatorSlotSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

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
