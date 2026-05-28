"""HTTP routers grouped by domain."""

from __future__ import annotations

from ditto.api_server.endpoints.health import router as health_router
from ditto.api_server.endpoints.metrics import router as metrics_router

__all__ = [
    "health_router",
    "metrics_router",
]
