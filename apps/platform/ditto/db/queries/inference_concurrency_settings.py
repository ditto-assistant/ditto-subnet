"""Append-only hosted-embedding concurrency reads and writes.

Backs ``ditto.api_server.endpoints.admin_inference_concurrency_settings``
(operator writes) and ``ditto.api_server.inference_concurrency_settings`` (the
admission-path read). The table is append-only by contract: this module never
UPDATEs or deletes a row, so the operator audit trail is complete and immutable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from ditto.db.models import InferenceConcurrencySettingsRevision

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"


async def latest_inference_concurrency_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> InferenceConcurrencySettingsRevision | None:
    """The newest revision for ``scope`` (the governing policy), or ``None``."""
    return await session.scalar(
        select(InferenceConcurrencySettingsRevision)
        .where(InferenceConcurrencySettingsRevision.scope == scope)
        .order_by(InferenceConcurrencySettingsRevision.revision.desc())
        .limit(1)
    )


async def list_inference_concurrency_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[InferenceConcurrencySettingsRevision]:
    """The append-only history, newest first (for the operator console)."""
    return list(
        await session.scalars(
            select(InferenceConcurrencySettingsRevision)
            .order_by(InferenceConcurrencySettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_inference_concurrency_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> InferenceConcurrencySettingsRevision:
    """Append one immutable revision (caller-managed transaction).

    Flushes immediately so a concurrent write racing the same
    ``(scope, parent_revision)`` surfaces as ``IntegrityError`` here (the caller
    maps it to a 409) rather than at commit.
    """
    row = InferenceConcurrencySettingsRevision(
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
