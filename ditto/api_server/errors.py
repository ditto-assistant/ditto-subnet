"""Exception hierarchy for :mod:`ditto.api_server`."""

from __future__ import annotations


class ApiServerError(Exception):
    """Base exception for :mod:`ditto.api_server`."""


# --- Configuration errors ---


class ApiServerConfigError(ApiServerError):
    """Raised when the API server cannot resolve a usable configuration.

    This can happen when:
    - A required environment variable for a sub-config is missing.
    - A resolved value falls outside its accepted range (e.g. negative
      port, unknown log level).
    """


# --- Lifespan errors ---


class ApiServerLifespanError(ApiServerError):
    """Raised when the FastAPI lifespan cannot open or close a dependency.

    This can happen when:
    - Postgres is unreachable during ``create_db_engine``.
    - Pylon refuses the chain client's credentials during
      ``ChainClient.__aenter__``.
    - A dependency's ``__aexit__`` raises while the lifespan is unwinding
      after a separate startup failure.
    """
