"""Tests for the append-only efficiency-bonus settings queries."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.efficiency_settings import (
    insert_efficiency_settings_revision,
    latest_efficiency_settings_revision,
    list_efficiency_settings_revisions,
)


async def _insert(
    session: AsyncSession, *, parent_revision: int, enabled: bool
) -> None:
    async with session.begin():
        await insert_efficiency_settings_revision(
            session,
            parent_revision=parent_revision,
            scope="*",
            settings={"enabled": enabled},
            checksum="a" * 64,
            reason="operator test change",
            actor="tester",
        )


class TestEfficiencySettingsQueries:
    async def test_latest_is_none_when_empty(self, session: AsyncSession) -> None:
        assert await latest_efficiency_settings_revision(session) is None
        assert await list_efficiency_settings_revisions(session) == []

    async def test_insert_and_read_latest(self, session: AsyncSession) -> None:
        await _insert(session, parent_revision=0, enabled=True)
        latest = await latest_efficiency_settings_revision(session)
        assert latest is not None
        assert latest.revision == 1
        assert latest.parent_revision == 0
        assert latest.settings == {"enabled": True}

    async def test_history_is_newest_first(self, session: AsyncSession) -> None:
        await _insert(session, parent_revision=0, enabled=True)
        await _insert(session, parent_revision=1, enabled=False)
        history = await list_efficiency_settings_revisions(session)
        assert [row.revision for row in history] == [2, 1]
        latest = await latest_efficiency_settings_revision(session)
        assert latest is not None and latest.revision == 2

    async def test_duplicate_parent_revision_conflicts(
        self, session: AsyncSession
    ) -> None:
        # Optimistic concurrency: two writers off the same parent collide on the
        # (scope, parent_revision) unique constraint — the endpoint maps this to
        # a 409 so a concurrent change is never silently clobbered.
        await _insert(session, parent_revision=0, enabled=True)
        with pytest.raises(IntegrityError):
            await _insert(session, parent_revision=0, enabled=False)
