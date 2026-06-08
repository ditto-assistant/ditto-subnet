"""Unit tests for :mod:`ditto.db.queries.agents`.

Exercises the real ORM + SQLite-in-memory engine so the
``session.add`` -> ``session.flush`` -> constraint-trip path is real,
not mocked. SQLite enforces UNIQUE + NOT NULL the same way Postgres
does, which is all this module's dispatch needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db import IntegrityError as DbIntegrityError
from ditto.db.models import Agent, AgentStatus
from ditto.db.queries.agents import (
    get_agent_by_id,
    get_latest_agent_by_hotkey,
    insert_agent,
)


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


async def _seed_agent(
    session: AsyncSession,
    *,
    agent_id: UUID | None = None,
    miner_hotkey: str = "5HKAlphaHotkey",
    name: str = "alpha-agent",
    sha256: str = "deadbeef" * 8,
    ip_address: str | None = "192.0.2.1",
    status: AgentStatus = AgentStatus.UPLOADED,
    created_at: datetime | None = None,
) -> Agent:
    """Insert one ``agents`` row and return it.

    Overrides ``created_at`` explicitly when the test needs to control
    ordering, otherwise lets the schema default fire.
    """
    row = Agent(
        agent_id=agent_id or uuid4(),
        miner_hotkey=miner_hotkey,
        name=name,
        sha256=sha256,
        ip_address=ip_address,
        status=status,
    )
    if created_at is not None:
        row.created_at = created_at
    async with session.begin():
        session.add(row)
    return row


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


class TestGetLatestAgentByHotkey:
    async def test_returns_single_agent(self, session: AsyncSession):
        seeded = await _seed_agent(session)
        result = await get_latest_agent_by_hotkey(
            session, miner_hotkey=seeded.miner_hotkey
        )
        assert result is not None
        assert result.agent_id == seeded.agent_id

    async def test_returns_most_recent_when_multiple(self, session: AsyncSession):
        """Three rows for the same hotkey, varied ``created_at``. The
        query must order DESC and take one."""
        now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        hotkey = "5HKMultiHotkey"
        await _seed_agent(
            session, miner_hotkey=hotkey, created_at=now - timedelta(days=2)
        )
        await _seed_agent(
            session, miner_hotkey=hotkey, created_at=now - timedelta(days=1)
        )
        latest = await _seed_agent(session, miner_hotkey=hotkey, created_at=now)

        result = await get_latest_agent_by_hotkey(session, miner_hotkey=hotkey)
        assert result is not None
        assert result.agent_id == latest.agent_id

    async def test_returns_none_when_no_match(self, session: AsyncSession):
        result = await get_latest_agent_by_hotkey(
            session, miner_hotkey="5HKHotkeyWithNoAgents"
        )
        assert result is None

    async def test_distinct_hotkeys_isolated(self, session: AsyncSession):
        """Other hotkeys' rows must not bleed into the lookup."""
        await _seed_agent(session, miner_hotkey="5HKHotkeyA")
        target = await _seed_agent(session, miner_hotkey="5HKHotkeyB")

        result = await get_latest_agent_by_hotkey(session, miner_hotkey="5HKHotkeyB")
        assert result is not None
        assert result.agent_id == target.agent_id

    async def test_status_unfiltered(self, session: AsyncSession):
        """A banned latest row is still the latest; no filter applies.
        Hotkey-level banned surfacing belongs to a later PR alongside
        the ``banned_hotkeys`` table."""
        hotkey = "5HKBannedHotkey"
        await _seed_agent(
            session,
            miner_hotkey=hotkey,
            created_at=datetime(2026, 6, 7, tzinfo=UTC),
        )
        latest_banned = await _seed_agent(
            session,
            miner_hotkey=hotkey,
            status=AgentStatus.BANNED,
            created_at=datetime(2026, 6, 8, tzinfo=UTC),
        )

        result = await get_latest_agent_by_hotkey(session, miner_hotkey=hotkey)
        assert result is not None
        assert result.agent_id == latest_banned.agent_id
        assert result.status == AgentStatus.BANNED


class TestGetAgentById:
    async def test_returns_agent_when_exists(self, session: AsyncSession):
        seeded = await _seed_agent(session)
        result = await get_agent_by_id(session, agent_id=seeded.agent_id)
        assert result is not None
        assert result.agent_id == seeded.agent_id

    async def test_returns_none_when_missing(self, session: AsyncSession):
        result = await get_agent_by_id(session, agent_id=uuid4())
        assert result is None
