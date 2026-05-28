"""Configuration model for the Postgres data layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import quote

from ditto.db.errors import DatabaseConnectionError


@dataclass(frozen=True)
class PostgresConfig:
    """Postgres connection configuration.

    Production services load this via :func:`parse_postgres_config_from_env`;
    tests build instances directly. Dev defaults in :file:`docker-compose.yml`
    line up with the ``POSTGRES_*`` block in :file:`.env.example`.
    """

    host: str
    """Postgres host. ``localhost`` when compose maps the port to the host."""

    port: int
    """Postgres TCP port. Default 5432."""

    user: str
    """Postgres role used for connections."""

    password: str = field(repr=False)
    """Postgres password for ``user``. Excluded from ``repr`` so logging
    the config does not surface the credential."""

    database: str
    """Database name the pool connects to."""

    pool_min_size: int = 2
    """Minimum connections held open by the pool."""

    pool_max_size: int = 10
    """Maximum connections. Acquirers block when the pool is exhausted."""

    command_timeout: float = 30.0
    """Per-command timeout in seconds. Long queries are cancelled at this limit."""

    @property
    def dsn(self) -> str:
        """asyncpg-compatible connection string built from the fields.

        User, password, and database are percent-encoded so reserved
        characters (``@``, ``:``, ``/``, ``#``) in secrets cannot
        corrupt the URL structure.
        """
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        database = quote(self.database, safe="")
        return f"postgresql://{user}:{password}@{self.host}:{self.port}/{database}"

    @property
    def async_dsn(self) -> str:
        """SQLAlchemy async DSN selecting the asyncpg driver.

        Same percent-encoding as :attr:`dsn`.
        """
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        database = quote(self.database, safe="")
        return (
            f"postgresql+asyncpg://{user}:{password}@{self.host}:{self.port}/{database}"
        )


def parse_postgres_config_from_env() -> PostgresConfig:
    """Build a :class:`PostgresConfig` from ``POSTGRES_*`` env vars.

    Required env vars: ``POSTGRES_USER``, ``POSTGRES_PASSWORD``, ``POSTGRES_DB``.
    All others fall back to safe defaults.

    Raises:
        DatabaseConnectionError: When any required env var is unset, or
            when a numeric env var (port, pool sizes, command timeout)
            cannot be parsed.
    """
    try:
        return PostgresConfig(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"],
            pool_min_size=int(os.environ.get("POSTGRES_POOL_MIN_SIZE", "2")),
            pool_max_size=int(os.environ.get("POSTGRES_POOL_MAX_SIZE", "10")),
            command_timeout=float(os.environ.get("POSTGRES_COMMAND_TIMEOUT", "30.0")),
        )
    except KeyError as e:
        raise DatabaseConnectionError(
            f"required postgres env var missing: {e.args[0]}"
        ) from e
    except ValueError as e:
        raise DatabaseConnectionError(f"invalid numeric postgres env var: {e}") from e
