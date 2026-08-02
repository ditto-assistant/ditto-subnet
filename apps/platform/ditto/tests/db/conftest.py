"""Shared builders for ditto.db tests.

Engine, session-maker, and session fixtures live in the root
:mod:`ditto.tests.conftest` and run against a real Postgres. This module
keeps only the builders that are specific to ``ditto.db``.
"""

from __future__ import annotations

from typing import Any

from ditto.db.config import PostgresConfig


def make_postgres_config(**overrides: Any) -> PostgresConfig:
    """Build a :class:`PostgresConfig` with sensible defaults.

    Defaults match the docker-compose service; tests override only the
    fields they care about.
    """
    base: dict[str, Any] = {
        "host": "localhost",
        "port": 5432,
        "user": "ditto",
        "password": "ditto",
        "database": "ditto",
        "pool_min_size": 2,
        "pool_max_size": 10,
        "command_timeout": 30.0,
    }
    base.update(overrides)
    return PostgresConfig(**base)
