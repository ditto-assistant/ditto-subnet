"""HTTP routers grouped by domain.

Ops endpoints (``health``, ``metrics``) sit at the root path. Business
endpoints (upload, validator, scoring, retrieval, admin) will mount
under ``/api/v1/`` when their PRs land, so consumer clients can rely
on a versioned consumer surface separate from ops infra.

Each submodule defines an :class:`fastapi.APIRouter` and exposes it as
``router``; :func:`ditto.api_server.factory.create_api_server` collects
and mounts them.
"""

from __future__ import annotations

from ditto.api_server.endpoints.health import router as health_router
from ditto.api_server.endpoints.metrics import router as metrics_router

__all__ = [
    "health_router",
    "metrics_router",
]
