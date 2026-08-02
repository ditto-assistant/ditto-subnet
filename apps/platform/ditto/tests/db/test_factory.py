"""Unit tests for ditto.db.factory."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ditto.db.errors import DatabaseConnectionError
from ditto.db.factory import create_db_engine, create_session_maker
from ditto.tests.db.conftest import make_postgres_config


class TestCreateDbEngine:
    """Tests for :func:`create_db_engine`."""

    def test_uses_env_when_no_config_passed(self, monkeypatch: pytest.MonkeyPatch):
        # Clear any ambient POSTGRES_* so the test runs hermetically: a
        # stale shell env (e.g. POSTGRES_PORT="bad") would otherwise make
        # parse_postgres_config_from_env fail for reasons unrelated to
        # the assertion below.
        for key in (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_DB",
            "POSTGRES_POOL_MIN_SIZE",
            "POSTGRES_POOL_MAX_SIZE",
            "POSTGRES_COMMAND_TIMEOUT",
        ):
            monkeypatch.delenv(key, raising=False)
        # Required env present so parse_postgres_config_from_env succeeds.
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DB", "d")

        engine = create_db_engine()

        assert isinstance(engine, AsyncEngine)
        # asyncpg dialect chosen via the SA URL.
        assert engine.url.get_backend_name() == "postgresql"
        assert engine.url.get_driver_name() == "asyncpg"

    def test_passes_explicit_config_through(self):
        config = make_postgres_config(user="custom", database="other")

        engine = create_db_engine(config)

        assert isinstance(engine, AsyncEngine)
        assert engine.url.username == "custom"
        assert engine.url.database == "other"

    def test_wraps_sqlalchemy_error_in_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def boom(*_args: object, **_kwargs: object) -> object:
            raise SQLAlchemyError("bad dsn")

        monkeypatch.setattr("ditto.db.factory.create_async_engine", boom)

        with pytest.raises(DatabaseConnectionError, match="failed to open engine"):
            create_db_engine(make_postgres_config())

    def test_wraps_oserror_in_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def boom(*_args: object, **_kwargs: object) -> object:
            raise OSError("connection refused")

        monkeypatch.setattr("ditto.db.factory.create_async_engine", boom)

        with pytest.raises(DatabaseConnectionError, match="failed to open engine"):
            create_db_engine(make_postgres_config())


class TestCreateSessionMaker:
    """Tests for :func:`create_session_maker`."""

    async def test_returns_async_sessionmaker_bound_to_engine(
        self, engine: AsyncEngine
    ):
        session_maker = create_session_maker(engine)

        assert isinstance(session_maker, async_sessionmaker)
        async with session_maker() as session:
            assert session.bind is engine
