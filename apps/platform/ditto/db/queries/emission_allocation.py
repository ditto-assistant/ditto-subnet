"""Append-only emission allocation reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import EmissionAllocationRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


async def latest_emission_allocation_revision(
    session: AsyncSession,
) -> EmissionAllocationRevision | None:
    return await session.scalar(
        select(EmissionAllocationRevision)
        .order_by(EmissionAllocationRevision.revision.desc())
        .limit(1)
    )


async def latest_emission_allocation_number(session: AsyncSession) -> int:
    """The newest allocation revision number; 0 before the first write."""
    value = await session.scalar(
        select(EmissionAllocationRevision.revision)
        .order_by(EmissionAllocationRevision.revision.desc())
        .limit(1)
    )
    return int(value or 0)


async def list_emission_allocation_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[EmissionAllocationRevision]:
    return list(
        await session.scalars(
            select(EmissionAllocationRevision)
            .order_by(EmissionAllocationRevision.revision.desc())
            .limit(limit)
        )
    )
