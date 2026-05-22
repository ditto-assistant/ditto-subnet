"""Exception hierarchy for :mod:`ditto.api_server`.

Boundary errors raised during startup, lifespan, and runtime middleware
handling. Endpoint-level errors that map to specific HTTP status codes
land in their feature modules (e.g. an ``UploadError`` in
:mod:`ditto.api_server.endpoints.upload`).
"""

from __future__ import annotations


class ApiServerError(Exception):
    """Base exception for :mod:`ditto.api_server`.

    Subclassed by every typed error raised inside the API process.
    Catching this base lets callers (tests, the ``__main__`` crash path,
    sibling modules) treat all api-server failures uniformly without
    blanket ``except Exception``.
    """


# --- Configuration errors ---


class ApiServerConfigError(ApiServerError):
    """Raised when the API server cannot resolve a usable configuration.

    This can happen when:
    - A required environment variable for a sub-config is missing
      (delegated to :func:`ditto.db.config.parse_postgres_config_from_env`
      or :func:`ditto.chain.models.parse_chain_config_from_env`, which
      raise their own typed errors that this exception wraps).
    - An argparse-resolved value falls outside its accepted range
      (e.g. negative port, unknown log level).
    - The git revision lookup is the only failure mode that does NOT
      raise this; the commit hash gracefully degrades to ``"unknown"``.
    """


# --- Lifespan errors ---


class ApiServerLifespanError(ApiServerError):
    """Raised when the FastAPI lifespan cannot open or close a dependency.

    This can happen when:
    - Postgres is unreachable when ``create_db_engine`` opens its first
      connection during startup.
    - Pylon refuses the chain client's open-access token or identity
      credentials during ``ChainClient.__aenter__``.
    - A dependency's ``__aexit__`` raises while the lifespan is unwinding
      after a separate startup failure (suppressed as a chained error
      so the original cause is preserved).
    """
