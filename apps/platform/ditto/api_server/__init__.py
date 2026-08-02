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

from fastapi import FastAPI

from ditto.api_server.config import (
    ApiServerConfig,
    EfficiencyBonusConfig,
    ScreenerAuthConfig,
    ValidatorCompatibilityConfig,
    parse_api_server_config_from_env,
)
from ditto.api_server.errors import (
    ApiServerConfigError,
    ApiServerError,
    ApiServerLifespanError,
)
from ditto.api_server.validator_names import ValidatorNamesConfig


def create_api_server(config: ApiServerConfig | None = None) -> FastAPI:
    """Build the API lazily, keeping submodule imports independent.

    Database query modules import small API helpers such as ``crn`` and
    ``inference_routing``. Importing the endpoint factory as a package side
    effect makes those helpers recursively import the entire query graph and
    creates order-dependent cycles. The public constructor remains identical;
    only its heavy dependency is deferred until it is actually called.
    """
    from ditto.api_server.factory import create_api_server as build_api_server

    return build_api_server(config)


__all__ = [
    # Main components
    "create_api_server",
    # Configuration
    "ApiServerConfig",
    "EfficiencyBonusConfig",
    "ScreenerAuthConfig",
    "ValidatorCompatibilityConfig",
    "ValidatorNamesConfig",
    "parse_api_server_config_from_env",
    # Errors
    "ApiServerError",
    "ApiServerConfigError",
    "ApiServerLifespanError",
]
