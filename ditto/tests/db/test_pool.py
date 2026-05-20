"""Unit tests for ditto.db.pool."""

from __future__ import annotations

import pytest

from ditto.db.pool import _create_pool
from ditto.tests.db.conftest import make_postgres_config


class TestCreatePool:
    """Tests for the internal :func:`_create_pool` helper."""

    async def test_passes_dsn_and_pool_params_to_asyncpg(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured: dict[str, object] = {}

        async def fake_create_pool(**kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        monkeypatch.setattr("ditto.db.pool.asyncpg.create_pool", fake_create_pool)
        config = make_postgres_config(
            host="db",
            port=1234,
            user="u",
            password="p",
            database="d",
            pool_min_size=3,
            pool_max_size=11,
            command_timeout=12.5,
        )

        result = await _create_pool(config)

        assert result is not None
        assert captured["dsn"] == "postgresql://u:p@db:1234/d"
        assert captured["min_size"] == 3
        assert captured["max_size"] == 11
        assert captured["command_timeout"] == 12.5

    async def test_propagates_underlying_error(self, monkeypatch: pytest.MonkeyPatch):
        async def boom(**_: object) -> object:
            raise OSError("connection refused")

        monkeypatch.setattr("ditto.db.pool.asyncpg.create_pool", boom)
        config = make_postgres_config()

        with pytest.raises(OSError, match="connection refused"):
            await _create_pool(config)
