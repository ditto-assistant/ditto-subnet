"""The local simulator wipe passes append-only history guards and restores them."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ditto.simulator.fabric import wipe_all


async def test_wipe_cascades_past_cla_bindings_and_restores_their_guard(
    session_maker,
) -> None:
    # TRUNCATE agents CASCADE reaches agent_cla_bindings, whose statement
    # guard fires even when the table is empty.
    async with session_maker() as session, session.begin():
        await wipe_all(session)
    async with session_maker() as session:
        assert await session.scalar(text("SELECT count(*) FROM agents")) == 0
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text("TRUNCATE agent_cla_bindings"))
        await session.rollback()
