"""Shadow champion history for the Season 2 reranker (SN-209), real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import RerankerChampion
from ditto.db.queries.reranker import (
    RerankerChampionChangedError,
    count_reranker_champions,
    crown_reranker_champion,
    get_current_reranker_champion,
    insert_reranker_submission,
    list_recent_scored_reranker_submissions,
    list_reranker_champions,
)

T0 = datetime(2026, 10, 13, 12, tzinfo=UTC)


async def _submission(session: AsyncSession, n: int) -> UUID:
    submission_id = uuid4()
    await insert_reranker_submission(
        session,
        submission_id=submission_id,
        miner_hotkey=f"5Miner{n}",
        miner_coldkey=None,
        upload_sha256=f"{10_000 + n:064x}",
        artifact_sha256=f"{n:064x}",
        contract_sha256="c" * 64,
        size_bytes=100,
        base_model="none",
        base_model_license="mit",
    )
    return submission_id


async def _crown(
    session: AsyncSession, submission_id: UUID, previous: UUID | None, at: datetime
) -> RerankerChampion:
    return await crown_reranker_champion(
        session,
        submission_id=submission_id,
        expected_previous=previous,
        contract_sha256="c" * 64,
        pools_sha256="d" * 64,
        comparison={"dethrones": True},
        now=at,
    )


async def test_crown_supersedes_the_current_champion(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        a, b = await _submission(session, 1), await _submission(session, 2)
        assert await get_current_reranker_champion(session) is None
        await _crown(session, a, None, T0)
    async with session_maker() as session, session.begin():
        await _crown(session, b, a, T0 + timedelta(hours=1))
    async with session_maker() as session:
        current = await get_current_reranker_champion(session)
        assert current is not None and current.submission_id == b
        assert current.previous_submission_id == a
        history = await list_reranker_champions(session, limit=10)
        assert [c.submission_id for c in history] == [b, a]
        assert history[1].superseded_at == T0 + timedelta(hours=1)
        assert await count_reranker_champions(session) == 2


async def test_a_moved_crown_is_not_overwritten(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        a = await _submission(session, 11)
        b = await _submission(session, 12)
        c = await _submission(session, 13)
        await _crown(session, a, None, T0)
    # b was compared against the incumbent, but a holds the crown now.
    with pytest.raises(RerankerChampionChangedError):
        async with session_maker() as session, session.begin():
            await _crown(session, b, None, T0 + timedelta(minutes=1))
    async with session_maker() as session, session.begin():
        await _crown(session, c, a, T0 + timedelta(minutes=2))
    async with session_maker() as session:
        assert [
            x.submission_id for x in await list_reranker_champions(session, limit=10)
        ] == [c, a]


async def test_history_is_append_only(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        a = await _submission(session, 21)
        crown = await _crown(session, a, None, T0)
        crown_id = crown.id
    for statement in (
        update(RerankerChampion)
        .where(RerankerChampion.id == crown_id)
        .values(comparison={"dethrones": False}),
        update(RerankerChampion)
        .where(RerankerChampion.id == crown_id)
        .values(superseded_at=T0, pools_sha256="e" * 64),
        delete(RerankerChampion).where(RerankerChampion.id == crown_id),
    ):
        with pytest.raises(DBAPIError, match="append-only|only gain"):
            async with session_maker() as session, session.begin():
                await session.execute(statement)
    # Stamping superseded_at once is allowed; a second stamp is not.
    async with session_maker() as session, session.begin():
        await session.execute(
            update(RerankerChampion)
            .where(RerankerChampion.id == crown_id)
            .values(superseded_at=T0 + timedelta(hours=1))
        )
    with pytest.raises(DBAPIError, match="only gain"):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(RerankerChampion)
                .where(RerankerChampion.id == crown_id)
                .values(superseded_at=T0 + timedelta(hours=2))
            )


async def test_at_most_one_current_champion(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        a, b = await _submission(session, 31), await _submission(session, 32)
        await _crown(session, a, None, T0)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            session.add(
                RerankerChampion(
                    submission_id=b,
                    contract_sha256="c" * 64,
                    pools_sha256="d" * 64,
                    comparison={},
                )
            )


async def test_scoring_lease_columns_are_consistent(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await _submission(session, 41)
    with pytest.raises(IntegrityError, match="scoring_lease"):
        async with session_maker() as session, session.begin():
            await session.execute(
                text("UPDATE reranker_submissions SET status = 'scoring'")
            )


async def test_recent_scored_rows_need_a_score(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await _submission(session, 51)
        assert await list_recent_scored_reranker_submissions(session, limit=5) == []
