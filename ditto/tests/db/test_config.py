"""Unit tests for ditto.db.config.

Scope: env-var parsing + DSN composition. Frozen-dataclass behaviour and
field-default declarations are guaranteed by Python and not tested.
"""

from __future__ import annotations

import pytest

from ditto.db.config import PostgresConfig, parse_postgres_config_from_env
from ditto.db.errors import DatabaseConnectionError


def _clear_postgres_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``POSTGRES_*`` env var so tests start from a known state."""
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


class TestPostgresConfigDsn:
    """Tests for :attr:`PostgresConfig.dsn`."""

    def test_dsn_includes_every_field(self):
        config = PostgresConfig(
            host="db.internal",
            port=6543,
            user="ditto",
            password="hunter2",
            database="ditto_prod",
        )
        assert config.dsn == "postgresql://ditto:hunter2@db.internal:6543/ditto_prod"


class TestPostgresConfigRepr:
    """Tests for :meth:`PostgresConfig.__repr__`."""

    def test_password_excluded_from_repr(self):
        config = PostgresConfig(
            host="db.internal",
            port=6543,
            user="ditto",
            password="hunter2",
            database="ditto_prod",
        )
        assert "hunter2" not in repr(config)
        assert "password" not in repr(config)


class TestParsePostgresConfigFromEnv:
    """Tests for :func:`parse_postgres_config_from_env`."""

    def test_required_only_uses_defaults_for_optional(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_postgres_env(monkeypatch)
        monkeypatch.setenv("POSTGRES_USER", "ditto")
        monkeypatch.setenv("POSTGRES_PASSWORD", "ditto")
        monkeypatch.setenv("POSTGRES_DB", "ditto")

        config = parse_postgres_config_from_env()

        assert config.host == "localhost"
        assert config.port == 5432
        assert config.user == "ditto"
        assert config.password == "ditto"
        assert config.database == "ditto"
        assert config.pool_min_size == 2
        assert config.pool_max_size == 10
        assert config.command_timeout == 30.0

    def test_all_options_picked_up(self, monkeypatch: pytest.MonkeyPatch):
        _clear_postgres_env(monkeypatch)
        monkeypatch.setenv("POSTGRES_HOST", "db.internal")
        monkeypatch.setenv("POSTGRES_PORT", "6543")
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "p")
        monkeypatch.setenv("POSTGRES_DB", "d")
        monkeypatch.setenv("POSTGRES_POOL_MIN_SIZE", "5")
        monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "25")
        monkeypatch.setenv("POSTGRES_COMMAND_TIMEOUT", "60")

        config = parse_postgres_config_from_env()

        assert config.host == "db.internal"
        assert config.port == 6543
        assert config.user == "u"
        assert config.password == "p"
        assert config.database == "d"
        assert config.pool_min_size == 5
        assert config.pool_max_size == 25
        assert config.command_timeout == 60.0

    @pytest.mark.parametrize(
        "missing",
        ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"],
    )
    def test_missing_required_raises_database_connection_error(
        self, monkeypatch: pytest.MonkeyPatch, missing: str
    ):
        _clear_postgres_env(monkeypatch)
        # Set all required, then knock out the parametrized one.
        monkeypatch.setenv("POSTGRES_USER", "ditto")
        monkeypatch.setenv("POSTGRES_PASSWORD", "ditto")
        monkeypatch.setenv("POSTGRES_DB", "ditto")
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(DatabaseConnectionError, match="postgres env var missing"):
            parse_postgres_config_from_env()
