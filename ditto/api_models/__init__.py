"""Pydantic wire shapes shared across HTTP boundary.

Every model in this package describes the JSON payload of a request or
response served by :mod:`ditto.api_server` (and consumed by the miner
CLI + validator daemon). Pydantic lives here and nowhere else: internal
configs / value objects / results use ``@dataclass(frozen=True)`` per the
code quality standards.

Models are organised per concern in submodules; this ``__init__`` only
re-exports.

Usage:
    from ditto.api_models import HealthResponse

    payload = HealthResponse(status="ok", db="ok", chain="ok", commit="abc...")
"""

from __future__ import annotations

from ditto.api_models.health import HealthResponse
from ditto.api_models.retrieval import AgentResponse, AgentStatusResponse
from ditto.api_models.upload import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)

__all__ = [
    "AgentResponse",
    "AgentStatusResponse",
    "EvalPricingResponse",
    "HealthResponse",
    "UploadAgentResponse",
    "UploadCheckRequest",
    "UploadCheckResponse",
]
