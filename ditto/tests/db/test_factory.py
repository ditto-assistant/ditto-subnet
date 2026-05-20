"""Unit tests for ditto.db.factory."""

from __future__ import annotations

import asyncpg
import pytest

from ditto.db.errors import DatabaseConnectionError
from ditto.db.factory import create_db_pool
from ditto.tests.db.conftest import make_postgres_config


class TestCreateDbPool:
    """Tests for :func:`create_db_pool`."""

    async def test_uses_env_when_no_config_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Required env present so parse_postgres_config_from_env succeeds.
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DB", "d")
        # Pool open is mocked so no real connection is attempted.
        monkeypatch.setattr(
            "ditto.db.factory._create_pool",
            _async_returning("POOL"),
        )

        result = await create_db_pool()

        assert result == "POOL"

    async def test_passes_explicit_config_through(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        seen: list[object] = []

        async def fake_create_pool(config: object) -> str:
            seen.append(config)
            return "POOL"

        monkeypatch.setattr("ditto.db.factory._create_pool", fake_create_pool)
        config = make_postgres_config(user="custom")

        result = await create_db_pool(config)

        assert result == "POOL"
        assert seen == [config]

    async def test_wraps_asyncpg_error_in_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async def boom(_: object) -> object:
            raise asyncpg.InvalidPasswordError("bad password")

        monkeypatch.setattr("ditto.db.factory._create_pool", boom)

        with pytest.raises(DatabaseConnectionError, match="failed to open pool"):
            await create_db_pool(make_postgres_config())

    async def test_wraps_oserror_in_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        async def boom(_: object) -> object:
            raise OSError("connection refused")

        monkeypatch.setattr("ditto.db.factory._create_pool", boom)

        with pytest.raises(DatabaseConnectionError, match="failed to open pool"):
            await create_db_pool(make_postgres_config())

    async def test_wraps_interface_error_in_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # asyncpg.InterfaceError is NOT a subclass of asyncpg.PostgresError,
        # so it needs its own catch in the factory. Bad host / closed pool
        # / unsupported feature surface as InterfaceError.
        async def boom(_: object) -> object:
            raise asyncpg.InterfaceError("pool already closed")

        monkeypatch.setattr("ditto.db.factory._create_pool", boom)

        with pytest.raises(DatabaseConnectionError, match="failed to open pool"):
            await create_db_pool(make_postgres_config())


def _async_returning(value: object):
    """Tiny helper to build an awaitable that returns ``value``."""

    async def inner(*_args: object, **_kwargs: object) -> object:
        return value

    return inner
