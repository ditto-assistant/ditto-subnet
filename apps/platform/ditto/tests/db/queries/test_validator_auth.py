"""Real-Postgres replay-guard and janitor concurrency tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ditto.db.models import ValidatorRequestNonce
from ditto.db.queries.validator_auth import (
    VALIDATOR_NONCE_JANITOR_LOCK_KEY,
    ValidatorRequestReplayError,
    consume_validator_nonce,
    delete_expired_validator_nonces,
)


async def test_signed_request_only_inserts_its_nonce(
    engine: AsyncEngine,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    now = datetime.now(UTC)
    try:
        async with session_maker() as session, session.begin():
            await consume_validator_nonce(
                session,
                nonce=uuid4(),
                validator_hotkey="5SignedRequest",
                now=now,
                expires_at=now + timedelta(minutes=5),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

    assert (
        sum(statement.lstrip().upper().startswith("INSERT") for statement in statements)
        == 1
    )
    assert not any("DELETE" in statement.upper() for statement in statements)


async def test_expired_guard_still_rejects_replay_until_janitor_deletes_it(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    nonce = uuid4()
    used_at = datetime.now(UTC) - timedelta(minutes=10)
    async with session_maker() as session, session.begin():
        await consume_validator_nonce(
            session,
            nonce=nonce,
            validator_hotkey="5ReplayGuard",
            now=used_at,
            expires_at=used_at + timedelta(minutes=5),
        )

    async with session_maker() as session, session.begin():
        with pytest.raises(ValidatorRequestReplayError):
            await consume_validator_nonce(
                session,
                nonce=nonce,
                validator_hotkey="5ReplayGuard",
                now=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

    async with session_maker() as session, session.begin():
        assert (
            await delete_expired_validator_nonces(
                session, now=datetime.now(UTC), limit=1
            )
            == 1
        )


async def test_janitor_is_bounded_and_preserves_unexpired_guards(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                ValidatorRequestNonce(
                    nonce=uuid4(),
                    validator_hotkey=f"5Expired{ordinal}",
                    used_at=now - timedelta(hours=1),
                    expires_at=now - timedelta(minutes=ordinal + 1),
                )
                for ordinal in range(5)
            ]
            + [
                ValidatorRequestNonce(
                    nonce=uuid4(),
                    validator_hotkey="5Current",
                    used_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
            ]
        )

    async with session_maker() as session, session.begin():
        assert await delete_expired_validator_nonces(session, now=now, limit=2) == 2
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(ValidatorRequestNonce)
            )
            == 4
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ValidatorRequestNonce)
                .where(ValidatorRequestNonce.expires_at >= now)
            )
            == 1
        )


async def test_busy_janitor_does_not_wait_or_multiply_the_batch(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    async with session_maker() as holder:
        await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": VALIDATOR_NONCE_JANITOR_LOCK_KEY},
        )
        async with session_maker() as contender, contender.begin():
            assert (
                await delete_expired_validator_nonces(
                    contender, now=datetime.now(UTC), limit=100
                )
                is None
            )
        await holder.rollback()
