"""Central FastAPI service for the Ditto subnet.

Hosts platform-only endpoints (upload, validator work distribution,
scoring, retrieval, admin) plus ops infra (``/health``, ``/metrics``).
Owns the asyncpg engine and Pylon-backed chain client on ``app.state``
through the FastAPI lifespan, so per-request ``Depends`` factories
share the same connections without re-opening anything.

This PR ships only the shell + ops endpoints + middleware; business
endpoints land per-feature in subsequent PRs.

Usage:
    from ditto.api_server import create_api_server, parse_api_server_config_from_env

    config = parse_api_server_config_from_env(commit_hash="...")
    app = create_api_server(config)
    # serve via `uvicorn` or hand to `httpx.AsyncClient` in tests
"""

from __future__ import annotations

from ditto.api_server.config import (
    ApiServerConfig,
    parse_api_server_config_from_env,
)
from ditto.api_server.errors import (
    ApiServerConfigError,
    ApiServerError,
    ApiServerLifespanError,
)
from ditto.api_server.factory import create_api_server

__all__ = [
    # Main components
    "create_api_server",
    # Configuration
    "ApiServerConfig",
    "parse_api_server_config_from_env",
    # Errors
    "ApiServerError",
    "ApiServerConfigError",
    "ApiServerLifespanError",
]
