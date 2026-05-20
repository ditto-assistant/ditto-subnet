"""Postgres data layer for the Ditto subnet.

asyncpg-backed connection pool, async-context-manager connection with
contextvar-based reuse, decorator-driven query helpers, and frozen
dataclass result types. The schema is owned by alembic migrations
under :file:`alembic/versions/`; this package never declares ORM
mappings and never imports SQLAlchemy.

Every platform-side module reads or writes through this package - the
API server's endpoints, the payment verifier, the validator session
manager, the scoring write-out, and the public retrieval endpoints all
go through ``DatabaseConnection`` and per-domain query files in
:mod:`ditto.db.queries`. Putting all SQL behind a single boundary keeps
"what queries does each feature run" greppable and means schema
migrations are the only place column shapes are described.

Usage:
    from ditto.db import (
        DatabaseConnection,
        create_db_pool,
        parse_postgres_config_from_env,
    )

    pool = await create_db_pool()
    async with DatabaseConnection(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agents WHERE agent_id = $1",
            agent_id,
        )
"""

from __future__ import annotations

from ditto.db.config import PostgresConfig, parse_postgres_config_from_env
from ditto.db.connection import DatabaseConnection, db_operation
from ditto.db.errors import (
    DatabaseConnectionError,
    DatabaseError,
    IntegrityError,
    QueryError,
)
from ditto.db.factory import create_db_pool
from ditto.db.models import Agent, AgentStatus, EvaluationPayment

__all__ = [
    # Configuration
    "PostgresConfig",
    "parse_postgres_config_from_env",
    # Connection
    "DatabaseConnection",
    "db_operation",
    # Factory
    "create_db_pool",
    # Result models
    "Agent",
    "AgentStatus",
    "EvaluationPayment",
    # Errors
    "DatabaseError",
    "DatabaseConnectionError",
    "QueryError",
    "IntegrityError",
]
