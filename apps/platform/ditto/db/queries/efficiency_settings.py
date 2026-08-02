"""Reads + append-only write for the hot-swappable efficiency-bonus policy.

Backs ``ditto.api_server.endpoints.admin_efficiency_bonus_settings`` (operator
writes) and ``ditto.api_server.efficiency_settings`` (the compute-time read).
The table is append-only by contract: this module never UPDATEs or deletes a
row, so the operator audit trail is complete and immutable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import EfficiencyBonusSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_efficiency_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> EfficiencyBonusSettingsRevision | None:
    """The newest revision for ``scope`` (the governing policy), or ``None``."""
    return await session.scalar(
        select(EfficiencyBonusSettingsRevision)
        .where(EfficiencyBonusSettingsRevision.scope == scope)
        .order_by(EfficiencyBonusSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_efficiency_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[EfficiencyBonusSettingsRevision]:
    """The append-only history, newest first (for the operator console)."""
    return list(
        await session.scalars(
            select(EfficiencyBonusSettingsRevision)
            .order_by(EfficiencyBonusSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_efficiency_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> EfficiencyBonusSettingsRevision:
    """Append one immutable revision (caller-managed transaction).

    Flushes immediately so a concurrent write racing the same
    ``(scope, parent_revision)`` surfaces as ``IntegrityError`` here (the caller
    maps it to a 409) rather than at commit.
    """
    row = EfficiencyBonusSettingsRevision(
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
