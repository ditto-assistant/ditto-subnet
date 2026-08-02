"""Errors raised by the Postgres data layer."""

from __future__ import annotations


class DatabaseError(Exception):
    """Base exception for ditto.db errors."""

    pass


# --- Connection errors ---


class DatabaseConnectionError(DatabaseError):
    """Raised when a Postgres connection cannot be established.

    Distinct from :class:`QueryError` so callers can distinguish "I can't
    talk to the database at all" from "the query I tried to run failed".

    This can happen when:
    - The Postgres service is not running or not reachable at ``host:port``.
    - The DSN credentials are wrong (auth failure on the underlying socket).
    - A required ``POSTGRES_*`` env var is missing when calling
      :func:`parse_postgres_config_from_env`.
    - The pool's new-connection timeout fired while waiting for a free slot.
    - The configured database does not exist on the server.
    """

    pass


# --- Query errors ---


class QueryError(DatabaseError):
    """Raised when a query fails for non-integrity reasons.

    This can happen when:
    - SQL syntax error in the query string (a bug in the caller).
    - A referenced table or column does not exist (schema drift from migrations).
    - The query exceeded ``command_timeout`` and Postgres killed it.
    - The connection dropped mid-query (server restart, network hiccup).
    """

    pass


# --- Integrity errors ---


class IntegrityError(DatabaseError):
    """Raised when a constraint violation occurs.

    Replay-protection failures on ``evaluation_payments`` surface as this
    error, so payment-verification callers should catch it explicitly
    rather than treating it as a generic query failure.

    This can happen when:
    - A unique constraint is violated, e.g. the ``evaluation_payments``
      composite primary key ``(block_hash, extrinsic_index)`` already
      exists (replay attempt).
    - A NOT NULL constraint is violated by an insert with a missing column.
    - A foreign key reference does not exist (e.g. inserting a payment
      whose ``agent_id`` was never written to ``agents``).
    - A CHECK constraint is violated.
    """

    pass
