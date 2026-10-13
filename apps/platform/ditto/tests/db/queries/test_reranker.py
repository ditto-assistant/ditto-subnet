"""Queries over ``reranker_submissions`` against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.db.models import RerankerSubmission
from ditto.db.queries.reranker import (
    DuplicateRerankerArtifactError,
    count_recent_reranker_submissions,
    count_reranker_submissions,
    count_reranker_submissions_by_status,
    get_reranker_submission,
    get_reranker_submission_by_artifact,
    insert_reranker_submission,
    list_reranker_submissions,
    lock_reranker_owner,
    oldest_recent_reranker_submission_at,
)


def _digest(n: int) -> str:
    return f"{n:064x}"


async def _insert(
    session: AsyncSession,
    *,
    n: int,
    hotkey: str = "5Alice",
    coldkey: str | None = "cold-a",
) -> UUID:
    submission_id = uuid4()
    await insert_reranker_submission(
        session,
        submission_id=submission_id,
        miner_hotkey=hotkey,
        miner_coldkey=coldkey,
        upload_sha256=_digest(10_000 + n),
        artifact_sha256=_digest(n),
        contract_sha256=_digest(99),
        size_bytes=100 + n,
        base_model="none",
        base_model_license="mit",
    )
    return submission_id


async def test_insert_defaults_and_lookups(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        submission_id = await _insert(session, n=1)

    async with session_maker() as session:
        row = await get_reranker_submission(session, submission_id=submission_id)
        by_artifact = await get_reranker_submission_by_artifact(
            session, artifact_sha256=_digest(1)
        )
    assert row is not None and by_artifact is not None
    assert by_artifact.id == submission_id
    assert row.status == "accepted"
    assert row.seed is None and row.score is None and row.scored_at is None
    assert row.created_at.tzinfo is not None


async def test_duplicate_artifact_raises_and_keeps_the_transaction_usable(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await _insert(session, n=1, hotkey="5Alice")
    async with session_maker() as session, session.begin():
        with pytest.raises(DuplicateRerankerArtifactError):
            await _insert(session, n=1, hotkey="5Bob", coldkey="cold-b")
        # The savepoint rolled back only the conflicting insert.
        await _insert(session, n=2, hotkey="5Bob", coldkey="cold-b")

    async with session_maker() as session:
        assert await count_reranker_submissions(session) == 2
        assert await count_reranker_submissions(session, miner_hotkey="5Bob") == 1


async def test_list_pages_newest_first_and_filters(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    base = datetime(2026, 10, 13, tzinfo=UTC)
    ids = []
    async with session_maker() as session, session.begin():
        for n in range(5):
            submission_id = await _insert(
                session, n=n, hotkey="5Alice" if n % 2 == 0 else "5Bob"
            )
            await session.execute(
                update(RerankerSubmission)
                .where(RerankerSubmission.id == submission_id)
                .values(created_at=base + timedelta(minutes=n))
            )
            ids.append(submission_id)

    async with session_maker() as session:
        first = await list_reranker_submissions(session, limit=2, offset=0)
        second = await list_reranker_submissions(session, limit=2, offset=2)
        alice = await list_reranker_submissions(
            session, limit=10, offset=0, miner_hotkey="5Alice"
        )
        accepted = await list_reranker_submissions(
            session, limit=10, offset=0, status="accepted"
        )
        scored = await list_reranker_submissions(
            session, limit=10, offset=0, status="scored"
        )
    assert [r.id for r in first] == [ids[4], ids[3]]
    assert [r.id for r in second] == [ids[2], ids[1]]
    assert [r.id for r in alice] == [ids[4], ids[2], ids[0]]
    assert len(accepted) == 5 and scored == []


async def test_status_counts_are_zero_filled(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        submission_id = await _insert(session, n=1)
        await _insert(session, n=2)
        await session.execute(
            update(RerankerSubmission)
            .where(RerankerSubmission.id == submission_id)
            .values(status="invalid", invalid_reason="latency gate")
        )
    async with session_maker() as session:
        counts = await count_reranker_submissions_by_status(session)
    assert counts == {
        "accepted": 1,
        "scoring": 0,
        "invalid": 1,
        "scored": 0,
        "failed": 0,
    }


async def test_status_check_constraint_rejects_unknown_status(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        submission_id = await _insert(session, n=1)
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                update(RerankerSubmission)
                .where(RerankerSubmission.id == submission_id)
                .values(status="uploaded")
            )


async def test_recent_counts_share_one_budget_per_coldkey(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        old = await _insert(session, n=1, hotkey="5Alice", coldkey="cold-a")
        await _insert(session, n=2, hotkey="5Alice2", coldkey="cold-a")
        await _insert(session, n=3, hotkey="5Carol", coldkey="cold-c")
        await session.execute(
            update(RerankerSubmission)
            .where(RerankerSubmission.id == old)
            .values(created_at=now - timedelta(days=2))
        )

    since = now - timedelta(hours=12)
    async with session_maker() as session:
        same_owner = await count_recent_reranker_submissions(
            session, since=since, miner_hotkey="5Alice", miner_coldkey="cold-a"
        )
        hotkey_only = await count_recent_reranker_submissions(
            session, since=since, miner_hotkey="5Alice", miner_coldkey=None
        )
        wide = await count_recent_reranker_submissions(
            session,
            since=now - timedelta(days=3),
            miner_hotkey="5Alice",
            miner_coldkey="cold-a",
        )
        oldest = await oldest_recent_reranker_submission_at(
            session, since=since, miner_hotkey="5Alice", miner_coldkey="cold-a"
        )
    assert same_owner == 1  # 5Alice2's row, through the shared coldkey
    assert hotkey_only == 0  # 5Alice's own row is outside the window
    assert wide == 2
    assert oldest is not None and oldest >= since


async def test_owner_lock_is_transaction_scoped(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as session, session.begin():
        await lock_reranker_owner(session, miner_hotkey="5Alice", miner_coldkey=None)
    # Released at commit: a second transaction takes it immediately.
    async with session_maker() as session, session.begin():
        await lock_reranker_owner(session, miner_hotkey="5Alice", miner_coldkey=None)
