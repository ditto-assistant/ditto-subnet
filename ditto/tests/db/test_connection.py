"""Unit tests for ditto.db.connection.

Connection-reuse semantics matter: the contextvar must hold the
outer-scope connection so nested ``DatabaseConnection`` blocks observe
it; only the outer scope releases back to the pool; the contextvar is
reset on outer exit so the next request starts clean.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ditto.db.connection import (
    DatabaseConnection,
    _connection_var,
    db_operation,
)


class TestDatabaseConnection:
    """Tests for :class:`DatabaseConnection`."""

    async def test_acquires_connection_when_no_outer_context(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        async with DatabaseConnection(mock_pool) as conn:
            assert conn is mock_conn
        mock_pool.acquire.assert_awaited_once()
        mock_pool.release.assert_awaited_once_with(mock_conn)

    async def test_reuses_connection_when_inside_outer_context(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        # The point of this test is that nesting must produce two *separate*
        # DatabaseConnection instances that share state via the contextvar.
        # Collapsing into one `async with` would defeat the assertion.
        async with DatabaseConnection(mock_pool) as outer:  # noqa: SIM117
            async with DatabaseConnection(mock_pool) as inner:
                assert inner is outer is mock_conn
        # Acquire fired only once - the inner scope reused.
        mock_pool.acquire.assert_awaited_once()
        # Release fired only once - the inner scope was a no-op on exit.
        mock_pool.release.assert_awaited_once_with(mock_conn)

    async def test_does_not_release_when_reused(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        async with DatabaseConnection(mock_pool):
            mock_pool.release.assert_not_awaited()
            async with DatabaseConnection(mock_pool):
                pass
            # Still no release after inner exit.
            mock_pool.release.assert_not_awaited()
        # After outer exit, exactly one release.
        mock_pool.release.assert_awaited_once_with(mock_conn)

    async def test_contextvar_cleared_after_exit(self, mock_pool: MagicMock):
        assert _connection_var.get() is None
        async with DatabaseConnection(mock_pool):
            assert _connection_var.get() is not None
        assert _connection_var.get() is None

    async def test_release_runs_even_when_body_raises(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        with pytest.raises(RuntimeError, match="boom"):
            async with DatabaseConnection(mock_pool):
                raise RuntimeError("boom")
        mock_pool.release.assert_awaited_once_with(mock_conn)
        assert _connection_var.get() is None


class TestDbOperationDecorator:
    """Tests for :func:`db_operation`."""

    async def test_handler_sees_active_connection(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        seen: list[object] = []

        @db_operation
        async def handler(_pool: object) -> str:
            seen.append(_connection_var.get())
            return "ok"

        result = await handler(mock_pool)

        assert result == "ok"
        assert seen == [mock_conn]
        mock_pool.acquire.assert_awaited_once()
        mock_pool.release.assert_awaited_once_with(mock_conn)

    async def test_propagates_return_value(self, mock_pool: MagicMock):
        @db_operation
        async def handler(_pool: object, x: int, y: int) -> int:
            return x + y

        assert await handler(mock_pool, 2, 3) == 5

    async def test_propagates_exception_and_releases(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        @db_operation
        async def handler(_pool: object) -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            await handler(mock_pool)
        mock_pool.release.assert_awaited_once_with(mock_conn)

    async def test_nested_decorated_calls_share_connection(
        self, mock_pool: MagicMock, mock_conn: AsyncMock
    ):
        @db_operation
        async def inner(_pool: object) -> object:
            return _connection_var.get()

        @db_operation
        async def outer(pool: object) -> tuple[object, object]:
            outer_conn = _connection_var.get()
            inner_conn = await inner(pool)
            return outer_conn, inner_conn

        outer_conn, inner_conn = await outer(mock_pool)

        assert outer_conn is mock_conn
        assert inner_conn is mock_conn
        mock_pool.acquire.assert_awaited_once()
        mock_pool.release.assert_awaited_once_with(mock_conn)
