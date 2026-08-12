"""Read helpers for audited validator fleet update operations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import ValidatorFleetUpdateOperation


async def latest_fleet_update_operation(
    session: AsyncSession,
) -> ValidatorFleetUpdateOperation | None:
    return await session.scalar(
        select(ValidatorFleetUpdateOperation)
        .order_by(
            ValidatorFleetUpdateOperation.created_at.desc(),
            ValidatorFleetUpdateOperation.operation_id.desc(),
        )
        .limit(1)
    )


async def pending_fleet_update_command(
    session: AsyncSession,
    *,
    validator_hotkey: str,
    acknowledged_operation_id: UUID | None,
) -> ValidatorFleetUpdateOperation | None:
    """Return the newest operation still awaiting this validator's receipt."""
    row = await latest_fleet_update_operation(session)
    if (
        row is None
        or validator_hotkey not in row.target_validator_hotkeys
        or row.operation_id == acknowledged_operation_id
    ):
        return None
    return row
