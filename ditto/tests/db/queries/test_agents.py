"""Unit tests for :mod:`ditto.db.queries.agents`.

Exercises the real ORM + SQLite-in-memory engine so the
``session.add`` -> ``session.flush`` -> constraint-trip path is real,
not mocked. SQLite enforces UNIQUE + NOT NULL the same way Postgres
does, which is all this module's dispatch needs.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db import IntegrityError as DbIntegrityError
from ditto.db.models import Agent, AgentStatus
from ditto.db.queries.agents import insert_agent


def _make_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent_id": uuid4(),
        "miner_hotkey": "5HKAlphaHotkey",
        "name": "alpha-agent",
        "sha256": "deadbeef" * 8,
        "ip_address": "192.0.2.1",
    }
    base.update(overrides)
    return base


class TestInsertAgentHappyPath:
    async def test_inserts_row(self, session: AsyncSession):
        kwargs = _make_kwargs()
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent).where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.miner_hotkey == kwargs["miner_hotkey"]
        assert row.name == kwargs["name"]
        assert row.sha256 == kwargs["sha256"]
        assert row.ip_address == kwargs["ip_address"]

    async def test_status_defaults_to_uploaded(self, session: AsyncSession):
        """The schema default places new rows in the initial state. The
        screener PR moves them forward; this PR must not bypass it."""
        kwargs = _make_kwargs()
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent).where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.status == AgentStatus.UPLOADED

    async def test_ip_address_optional(self, session: AsyncSession):
        kwargs = _make_kwargs(ip_address=None)
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent).where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.ip_address is None


class TestInsertAgentConstraintViolations:
    async def test_duplicate_agent_id_rejected(self, session: AsyncSession):
        agent_id = uuid4()
        async with session.begin():
            await insert_agent(session, **_make_kwargs(agent_id=agent_id))  # type: ignore[arg-type]

        with pytest.raises(DbIntegrityError):
            async with session.begin():
                await insert_agent(session, **_make_kwargs(agent_id=agent_id))  # type: ignore[arg-type]

    async def test_error_chains_original_cause(self, session: AsyncSession):
        agent_id = uuid4()
        async with session.begin():
            await insert_agent(session, **_make_kwargs(agent_id=agent_id))  # type: ignore[arg-type]

        with pytest.raises(DbIntegrityError) as info:
            async with session.begin():
                await insert_agent(session, **_make_kwargs(agent_id=agent_id))  # type: ignore[arg-type]
        # ``raise X from e`` chains via ``__cause__``; the original SA
        # IntegrityError must remain reachable for debugging.
        assert info.value.__cause__ is not None


class TestKeywordOnlyContract:
    async def test_positional_args_rejected(self, session: AsyncSession):
        """All non-session args must be keyword-only so callers can't
        accidentally swap UUID + hotkey."""
        with pytest.raises(TypeError):
            await insert_agent(  # type: ignore[misc]
                session,
                uuid4(),
                "5HKsomething",
                "name",
                "deadbeef" * 8,
                None,
            )
