"""Append-only queue-policy settings reads and writes.

Backs ``ditto.api_server.endpoints.admin_queue_policy_settings`` (operator
writes) and ``ditto.api_server.queue_policy_settings`` (the rollout-start and
hot-path reads). The table is append-only by contract: this module never
UPDATEs or deletes a row, so the operator audit trail is complete and immutable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import QueuePolicySettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_queue_policy_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> QueuePolicySettingsRevision | None:
    """The newest revision for ``scope`` (the governing policy), or ``None``."""
    return await session.scalar(
        select(QueuePolicySettingsRevision)
        .where(QueuePolicySettingsRevision.scope == scope)
        .order_by(QueuePolicySettingsRevision.revision.desc())
        .limit(1)
    )


async def list_queue_policy_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[QueuePolicySettingsRevision]:
    """The append-only history, newest first (for the operator console)."""
    return list(
        await session.scalars(
            select(QueuePolicySettingsRevision)
            .order_by(QueuePolicySettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_queue_policy_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> QueuePolicySettingsRevision:
    """Append one immutable revision (caller-managed transaction).

    Flushes immediately so a concurrent write racing the same
    ``(scope, parent_revision)`` surfaces as ``IntegrityError`` here (the caller
    maps it to a 409) rather than at commit.
    """
    row = QueuePolicySettingsRevision(
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
