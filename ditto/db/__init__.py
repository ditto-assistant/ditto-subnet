"""Postgres data layer for the Ditto subnet.

SQLAlchemy 2.0 async ORM over the asyncpg driver. Alembic migrations
under :file:`alembic/versions/` own the schema; declarative models in
:mod:`ditto.db.models` describe it in Python so :class:`AsyncSession`
queries hydrate into typed objects. All platform-side SQL goes through
this package.

Usage:
    from ditto.db import (
        create_db_engine,
        create_session_maker,
        parse_postgres_config_from_env,
    )

    engine = create_db_engine()
    session_maker = create_session_maker(engine)
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
    await engine.dispose()
"""

from __future__ import annotations

from ditto.db.config import PostgresConfig, parse_postgres_config_from_env
from ditto.db.errors import (
    DatabaseConnectionError,
    DatabaseError,
    IntegrityError,
    QueryError,
)
from ditto.db.factory import create_db_engine, create_session_maker
from ditto.db.models import Agent, AgentStatus, Base, EvaluationPayment

__all__ = [
    # Configuration
    "PostgresConfig",
    "parse_postgres_config_from_env",
    # Factory
    "create_db_engine",
    "create_session_maker",
    # Declarative base + result models
    "Base",
    "Agent",
    "AgentStatus",
    "EvaluationPayment",
    # Errors
    "DatabaseError",
    "DatabaseConnectionError",
    "QueryError",
    "IntegrityError",
]
