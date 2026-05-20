"""Configuration model for the Postgres data layer."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
        """asyncpg-compatible connection string built from the fields."""
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


def parse_postgres_config_from_env() -> PostgresConfig:
    """Build a :class:`PostgresConfig` from ``POSTGRES_*`` env vars.

    Required env vars: ``POSTGRES_USER``, ``POSTGRES_PASSWORD``, ``POSTGRES_DB``.
    All others fall back to safe defaults.

    Raises:
        DatabaseConnectionError: When any required env var is unset.
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
