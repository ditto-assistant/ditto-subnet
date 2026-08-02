"""Wire shapes for the ``/retrieval/*`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.agent_status import AgentStatus


class AgentResponse(BaseModel):
    """Returned by ``GET /retrieval/agent-by-hotkey``.

    Mirrors the public-safe shape of the ``agents`` row. Client-side
    connection metadata (the request's source IP) is never retained or
    surfaced; the corresponding column was dropped from the schema once
    no read path consumed it.
    """

    agent_id: Annotated[UUID, Field(description="Server-generated agent identifier.")]
    miner_hotkey: Annotated[str, Field(description="Submitting miner's SS58 hotkey.")]
    name: Annotated[str, Field(description="Miner-chosen agent name.")]
    version: Annotated[
        int | None,
        Field(
            default=None,
            ge=1,
            description=(
                "Submission version within this named agent; null for legacy uploads."
            ),
        ),
    ] = None
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state (see MVP-SPEC §9).")
    ]
    sha256: Annotated[
        str, Field(description="SHA-256 of the uploaded tarball, lowercase hex.")
    ]
    created_at: Annotated[
        datetime, Field(description="When the upload row was inserted (UTC).")
    ]
    screening_reason: Annotated[
        str | None,
        Field(
            description="Public-safe reason for the current screening outcome.",
        ),
    ] = None
    screening_reason_code: Annotated[
        str | None,
        Field(description="Stable machine-readable screening outcome code."),
    ] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "miner_hotkey": "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                "name": "alpha-agent",
                "version": 1,
                "status": "uploaded",
                "sha256": "deadbeef" * 8,
                "created_at": "2026-06-08T12:00:00Z",
                "screening_reason": None,
            }
        }
    )


class AgentStatusResponse(BaseModel):
    """Returned by ``GET /retrieval/agent/{agent_id}/status``.

    Minimal shape so polling loops carry small bodies. The caller already
    knows ``agent_id`` from the upload response; the field is echoed for
    self-containment but no extra metadata is emitted.
    """

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    status: Annotated[AgentStatus, Field(description="Lifecycle state.")]
    screening_reason: Annotated[
        str | None,
        Field(
            description="Public-safe reason for the current screening outcome.",
        ),
    ] = None
    screening_reason_code: Annotated[
        str | None,
        Field(description="Stable machine-readable screening outcome code."),
    ] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "screening",
                "screening_reason": None,
            }
        }
    )
