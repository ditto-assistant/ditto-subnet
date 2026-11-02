"""Append-only scoring lease clock reads and writes (#1156).

Backs ``ditto.api_server.endpoints.admin_scoring_lease_settings`` (operator
writes) and ``ditto.api_server.scoring_lease_settings`` (the ticket-issue read).
This module never UPDATEs or deletes a row, so the audit trail is complete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import ScoringLeaseSettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_scoring_lease_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> ScoringLeaseSettingsRevision | None:
    """The newest revision for ``scope`` (the governing policy), or ``None``."""
    return await session.scalar(
        select(ScoringLeaseSettingsRevision)
        .where(ScoringLeaseSettingsRevision.scope == scope)
        .order_by(ScoringLeaseSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_scoring_lease_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[ScoringLeaseSettingsRevision]:
    """The append-only history, newest first (for the operator console)."""
    return list(
        await session.scalars(
            select(ScoringLeaseSettingsRevision)
            .order_by(ScoringLeaseSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_scoring_lease_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> ScoringLeaseSettingsRevision:
    """Append one immutable revision (caller-managed transaction).

    Flushes immediately so a concurrent write racing the same
    ``(scope, parent_revision)`` surfaces as ``IntegrityError`` here.
    """
    row = ScoringLeaseSettingsRevision(
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
