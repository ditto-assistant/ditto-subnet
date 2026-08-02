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
from sqlalchemy.orm import undefer_group

from ditto.db import IntegrityError as DbIntegrityError
from ditto.db.models import Agent, AgentStatus, EvaluationPayment
from ditto.db.queries.agents import (
    SubmissionCooldownError,
    enforce_submission_cooldown,
    get_agent_by_id,
    get_latest_agent_by_hotkey,
    insert_agent,
    resolve_review,
)


def _make_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent_id": uuid4(),
        "miner_hotkey": "5HKAlphaHotkey",
        "name": "alpha-agent",
        "sha256": "deadbeef" * 8,
        "size_bytes": 524288,
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
        status=status,
    )
    if created_at is not None:
        row.created_at = created_at
    async with session.begin():
        session.add(row)
    return row


async def _seed_payment_owner(
    session: AsyncSession,
    *,
    agent: Agent,
    miner_coldkey: str = "5CKOwner",
) -> None:
    async with session.begin():
        session.add(
            EvaluationPayment(
                block_hash="0x" + agent.agent_id.hex,
                extrinsic_index=0,
                agent_id=agent.agent_id,
                miner_hotkey=agent.miner_hotkey,
                miner_coldkey=miner_coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=datetime.now(UTC),
            )
        )


class TestInsertAgentHappyPath:
    async def test_inserts_row(self, session: AsyncSession):
        kwargs = _make_kwargs()
        async with session.begin():
            version = await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent).where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.miner_hotkey == kwargs["miner_hotkey"]
        assert row.name == kwargs["name"]
        assert row.sha256 == kwargs["sha256"]
        assert version == 1
        assert row.version == 1

    async def test_versions_repeat_names_per_hotkey(self, session: AsyncSession):
        hotkey = "5HKVersionedHotkey"
        async with session.begin():
            first = await insert_agent(
                session,
                agent_id=uuid4(),
                miner_hotkey=hotkey,
                name="memory",
                sha256="11" * 32,
                size_bytes=1,
            )
        async with session.begin():
            second = await insert_agent(
                session,
                agent_id=uuid4(),
                miner_hotkey=hotkey,
                name="memory",
                sha256="22" * 32,
                size_bytes=2,
            )
        async with session.begin():
            other_name = await insert_agent(
                session,
                agent_id=uuid4(),
                miner_hotkey=hotkey,
                name="tools",
                sha256="33" * 32,
                size_bytes=3,
            )

        assert (first, second, other_name) == (1, 2, 1)

    async def test_legacy_rows_remain_unversioned_and_new_series_starts_at_one(
        self, session: AsyncSession
    ) -> None:
        hotkey = "5HKLegacyHotkey"
        legacy = await _seed_agent(session, miner_hotkey=hotkey, name="memory")
        assert legacy.version is None

        async with session.begin():
            first_versioned = await insert_agent(
                session,
                agent_id=uuid4(),
                miner_hotkey=hotkey,
                name="memory",
                sha256="44" * 32,
                size_bytes=4,
            )

        assert first_versioned == 1

    async def test_persists_normalized_source_hash(self, session: AsyncSession):
        # The exact-repack hash computed at upload must round-trip to the row.
        kwargs = _make_kwargs(normalized_source_hash="ns" * 32)
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent).where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.normalized_source_hash == "ns" * 32

    async def test_persists_prompt_fingerprint(self, session: AsyncSession):
        # The prompt sketch computed at upload must round-trip to the row.
        sketch = {"v": "p1", "k": 256, "card": 2, "m": ["aa", "bb"]}
        kwargs = _make_kwargs(prompt_fingerprint=sketch)
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                # The sketch columns are deferred; readers must undefer.
                select(Agent)
                .options(undefer_group("anticopy"))
                .where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.prompt_fingerprint == sketch

    async def test_persists_code_embedding(self, session: AsyncSession):
        # The code-embedding vector + model tag computed at upload must round-trip to
        # the row.
        kwargs = _make_kwargs(
            code_embedding=[0.1, 0.2, 0.3],
            code_embed_model="stub@test",
        )
        async with session.begin():
            await insert_agent(session, **kwargs)  # type: ignore[arg-type]

        row = (
            await session.execute(
                select(Agent)
                .options(undefer_group("anticopy"))
                .where(Agent.agent_id == kwargs["agent_id"])
            )
        ).scalar_one()
        assert row.code_embedding == [0.1, 0.2, 0.3]
        assert row.code_embed_model == "stub@test"

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


class TestSubmissionCooldown:
    async def test_rejects_until_one_hour_after_latest_submission(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        agent = await _seed_agent(session, created_at=now - timedelta(minutes=15))
        await _seed_payment_owner(session, agent=agent)

        with pytest.raises(SubmissionCooldownError) as info:
            async with session.begin():
                await enforce_submission_cooldown(
                    session,
                    miner_coldkey="5CKOwner",
                    cooldown=timedelta(hours=1),
                )

        assert info.value.retry_at >= now + timedelta(minutes=44)

    async def test_allows_after_one_hour(self, session: AsyncSession) -> None:
        agent = await _seed_agent(
            session,
            created_at=datetime.now(UTC) - timedelta(hours=1, seconds=1),
        )
        await _seed_payment_owner(session, agent=agent)

        async with session.begin():
            await enforce_submission_cooldown(
                session,
                miner_coldkey="5CKOwner",
                cooldown=timedelta(hours=1),
            )


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
            await insert_agent(  # type: ignore[misc, call-arg]
                session,
                uuid4(),
                "5HKsomething",
                "name",
                "deadbeef" * 8,
                524288,
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


async def _seed_held(session: AsyncSession, *, dup_of: UUID) -> Agent:
    agent = await _seed_agent(session, status=AgentStatus.ATH_PENDING_REVIEW)
    async with session.begin():
        agent.duplicate_of = dup_of
        agent.review_reason = "exact sha256 match"
    return agent


class TestResolveReview:
    async def test_clear_returns_to_scored_and_wipes_record(
        self, session: AsyncSession
    ):
        original = await _seed_agent(session)
        held = await _seed_held(session, dup_of=original.agent_id)
        async with session.begin():
            updated = await resolve_review(
                session, agent_id=held.agent_id, decision=AgentStatus.SCORED
            )
        assert updated is not None
        assert updated.status == AgentStatus.SCORED
        assert updated.duplicate_of is None
        assert updated.review_reason is None

    async def test_ban_keeps_moderation_record(self, session: AsyncSession):
        original = await _seed_agent(session)
        held = await _seed_held(session, dup_of=original.agent_id)
        async with session.begin():
            updated = await resolve_review(
                session, agent_id=held.agent_id, decision=AgentStatus.BANNED
            )
        assert updated is not None
        assert updated.status == AgentStatus.BANNED
        assert updated.duplicate_of == original.agent_id
        assert updated.review_reason == "exact sha256 match"

    async def test_unknown_agent_returns_none(self, session: AsyncSession):
        async with session.begin():
            result = await resolve_review(
                session, agent_id=uuid4(), decision=AgentStatus.SCORED
            )
        assert result is None

    async def test_not_held_agent_raises(self, session: AsyncSession):
        agent = await _seed_agent(session, status=AgentStatus.SCORED)
        with pytest.raises(ValueError, match="not ath_pending_review"):
            async with session.begin():
                await resolve_review(
                    session, agent_id=agent.agent_id, decision=AgentStatus.SCORED
                )

    async def test_invalid_decision_raises(self, session: AsyncSession):
        original = await _seed_agent(session)
        held = await _seed_held(session, dup_of=original.agent_id)
        with pytest.raises(ValueError, match="scored or banned"):
            async with session.begin():
                await resolve_review(
                    session, agent_id=held.agent_id, decision=AgentStatus.LIVE
                )
