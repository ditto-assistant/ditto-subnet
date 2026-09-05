"""Injected Platform-only grant-store adapter, with a fresh transaction per read."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.coding_hosted_private import PrivateV2ObjectGrant
from ditto.db.queries.coding_hosted_private import active_hosted_object_grant


class HostedPrivateGrantStore:
    """Trusted runtime binds worker and audience; validators get no constructor/API."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        audience: Literal["platform-authoring", "platform-grading"],
    ) -> None:
        if (
            not isinstance(worker_id, UUID)
            or worker_id.int == 0
            or audience not in {"platform-authoring", "platform-grading"}
        ):
            raise ValueError("hosted grant-store configuration is invalid")
        self._sessions, self._worker_id, self._audience = sessions, worker_id, audience

    async def active_grant(
        self, *, grant_id: UUID, audience: str
    ) -> PrivateV2ObjectGrant | None:
        if audience != self._audience:
            return None
        async with self._sessions() as session, session.begin():
            return await active_hosted_object_grant(
                session,
                grant_id=grant_id,
                worker_id=self._worker_id,
                audience=self._audience,
            )
