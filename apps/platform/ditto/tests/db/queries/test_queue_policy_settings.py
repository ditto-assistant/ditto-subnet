from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.queries.queue_policy_settings import (
    insert_queue_policy_settings_revision,
    latest_queue_policy_settings_revision,
    list_queue_policy_settings_revisions,
)

pytestmark = pytest.mark.asyncio


async def _insert(
    session: AsyncSession, *, parent_revision: int, rescore_cohort_size: int
) -> None:
    async with session.begin():
        await insert_queue_policy_settings_revision(
            session,
            parent_revision=parent_revision,
            scope="*",
            settings={"rescore_cohort_size": rescore_cohort_size},
            checksum="a" * 64,
            reason="widen the rescore cohort as the subnet scales",
            actor="tester",
        )


class TestQueuePolicySettingsQueries:
    async def test_latest_is_none_when_empty(self, session: AsyncSession) -> None:
        assert await latest_queue_policy_settings_revision(session) is None

    async def test_insert_and_read_latest(self, session: AsyncSession) -> None:
        await _insert(session, parent_revision=0, rescore_cohort_size=25)
        row = await latest_queue_policy_settings_revision(session)
        assert row is not None
        assert row.revision == 1
        assert row.parent_revision == 0
        assert row.settings == {"rescore_cohort_size": 25}

    async def test_history_is_newest_first(self, session: AsyncSession) -> None:
        await _insert(session, parent_revision=0, rescore_cohort_size=15)
        await _insert(session, parent_revision=1, rescore_cohort_size=25)
        history = await list_queue_policy_settings_revisions(session)
        assert [row.revision for row in history] == [2, 1]

    async def test_duplicate_parent_revision_conflicts(
        self, session: AsyncSession
    ) -> None:
        # Optimistic concurrency: two operators writing off the same parent
        # collide on the (scope, parent_revision) unique constraint, which the
        # endpoint maps to a 409 rather than silently clobbering one write.
        await _insert(session, parent_revision=0, rescore_cohort_size=15)
        with pytest.raises(IntegrityError):
            await _insert(session, parent_revision=0, rescore_cohort_size=25)
