"""Configuration for :mod:`ditto.api_server`.

Holds API-level settings (host, port, log level, commit hash) plus the
sub-configs the lifespan opens dependencies against. argparse + boot-time
flow lives in :mod:`ditto.api_server.__main__`; this module is the
dataclass + env-builder pair every test, factory, and CLI entry point
reuses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ditto.api_server.errors import ApiServerConfigError
from ditto.chain import ChainConfig, parse_chain_config_from_env
from ditto.db import PostgresConfig, parse_postgres_config_from_env


@dataclass(frozen=True)
class ApiServerConfig:
    """Resolved configuration for the API server process.

    Composition over flattening: ``postgres`` and ``chain`` carry their
    own typed dataclasses so the same configs feed validator daemon +
    smoke scripts unchanged.
    """

    host: str
    """Interface to bind. ``0.0.0.0`` for compose / cloud, ``127.0.0.1`` locally."""

    port: int
    """TCP port. Defaults to 8000; Pylon shifts to 8001 in compose."""

    log_level: str
    """Root logger level. One of the stdlib level names (``DEBUG``, ``INFO``,
    ``WARNING``, ``ERROR``, ``CRITICAL``)."""

    commit_hash: str
    """Git revision the process was built from, or ``"unknown"`` outside a checkout.

    Resolved by :mod:`ditto.api_server.__main__` via ``git rev-parse HEAD``
    before the FastAPI app is built, so :func:`create_api_server` can stash
    it on ``app.state.commit_hash`` for the ``/health`` endpoint.
    """

    postgres: PostgresConfig
    """Connection parameters for the platform database."""

    chain: ChainConfig
    """Pylon + subtensor settings for chain reads."""


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def parse_api_server_config_from_env(commit_hash: str) -> ApiServerConfig:
    """Build an :class:`ApiServerConfig` from the ``API_*`` env vars plus
    the postgres + chain sub-config parsers.

    Pure construction: this function does NOT validate range / set
    membership. Call :func:`check_config` after parsing to surface
    impossible combinations (port out of range, unknown log level).
    Splitting the two matches the standards-doc pattern where
    ``check_config`` is a separate gate.

    Args:
        commit_hash: Pre-resolved git revision. Passed in instead of read
            from env because ``git rev-parse`` is the source of truth and
            happens once at process start, not on every config rebuild.

    Raises:
        ApiServerConfigError: When ``API_PORT`` is not parseable as an
            integer (a parse-time failure, not a range check). Errors
            from the sub-config parsers (``DatabaseConnectionError``,
            ``ValueError`` from ``ChainConfig.__post_init__``) propagate
            untouched so callers see the original cause.
    """
    host = os.environ.get("API_HOST", "0.0.0.0")
    raw_port = os.environ.get("API_PORT", "8000")
    log_level = os.environ.get("API_LOG_LEVEL", "INFO").upper()

    try:
        port = int(raw_port)
    except ValueError as e:
        raise ApiServerConfigError(
            f"API_PORT must be an integer, got {raw_port!r}"
        ) from e

    return ApiServerConfig(
        host=host,
        port=port,
        log_level=log_level,
        commit_hash=commit_hash,
        postgres=parse_postgres_config_from_env(),
        chain=parse_chain_config_from_env(),
    )


def check_config(config: ApiServerConfig) -> None:
    """Validate a resolved :class:`ApiServerConfig`.

    Catches combinations the dataclass type system cannot:
    out-of-range ports and unrecognised log levels. Called by
    :mod:`ditto.api_server.__main__` after parsing + argparse overlay
    so CLI overrides are validated alongside env-resolved values.

    Raises:
        ApiServerConfigError: When ``port`` falls outside ``1..65535``
            or ``log_level`` is not a stdlib level name.
    """
    if not 1 <= config.port <= 65535:
        raise ApiServerConfigError(f"port out of range: {config.port}")
    if config.log_level not in _VALID_LOG_LEVELS:
        raise ApiServerConfigError(
            f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}; "
            f"got {config.log_level!r}"
        )
