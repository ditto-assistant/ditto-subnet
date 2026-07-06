"""Wire shapes for the ``/screener/*`` endpoints (screener-worker copy).

Mirrors ``ditto-platform``'s ``ditto/api_models/screener.py`` — there is no
shared package between the repos, so this copy is kept structurally in sync with
the platform contract (a drift guard lives in ``ditto/tests/contract/``). The
screener worker (:mod:`ditto.screener`) consumes these to talk to the platform's
thin state-machine endpoints:

1. ``GET  /screener/queue`` — list agents awaiting screening (``uploaded``).
2. ``GET  /screener/agent/{id}/artifact`` — fetch a download URL for the tarball.
3. ``POST /screener/agent/{id}/result`` — report the pass/fail verdict.

``ArtifactResponse`` (the ``/artifact`` shape) is shared with the validator and
lives in :mod:`ditto.api_models.validator`; import it from there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.upload import (
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)


class ScreenerQueueItem(BaseModel):
    """One agent awaiting screening, returned by ``GET /screener/queue``."""

    agent_id: Annotated[UUID, Field(description="Server-generated agent identifier.")]
    miner_hotkey: Annotated[str, Field(description="Submitting miner's SS58 hotkey.")]
    name: Annotated[str, Field(description="Miner-chosen agent name.")]
    sha256: Annotated[
        str, Field(description="SHA-256 of the uploaded tarball, lowercase hex.")
    ]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state at queue read time.")
    ]
    created_at: Annotated[
        datetime, Field(description="When the upload row was inserted (UTC).")
    ]


class ScreenerQueueResponse(BaseModel):
    """Returned by ``GET /screener/queue``; ``items`` is oldest-first."""

    items: Annotated[
        list[ScreenerQueueItem],
        Field(description="Agents awaiting screening, oldest first."),
    ]
    count: Annotated[int, Field(ge=0, description="Number of items returned.")]


class ScreenResultRequest(BaseModel):
    """Body of ``POST /screener/agent/{agent_id}/result``.

    The screener authenticates by signing the verdict: the signature is over the
    UTF-8 bytes of ``f"{screener_hotkey}:{agent_id}:{passed}"`` with the
    screener's hotkey keypair. Binding ``passed`` means a captured result cannot
    be replayed with the boolean flipped. ``True`` promotes the agent to
    ``evaluating``; ``False`` moves it to ``screening_failed``.
    """

    screener_hotkey: Annotated[
        str,
        Field(pattern=_SS58_PATTERN, description="Reporting screener's SS58 hotkey."),
    ]
    signature: Annotated[
        str,
        Field(
            pattern=_SIGNATURE_HEX_PATTERN,
            description=(
                "Hex sr25519 signature over ``{screener_hotkey}:{agent_id}:{passed}``."
            ),
        ),
    ]
    passed: Annotated[
        bool,
        Field(description="True promotes to evaluating; False -> screening_failed."),
    ]
    detail: Annotated[
        str,
        Field(
            default="",
            max_length=4000,
            description="Optional reason / build-log tail (logged, not persisted).",
        ),
    ]


class ScreenResultResponse(BaseModel):
    """Returned by ``POST /screener/agent/{agent_id}/result``.

    ``status`` is the agent's lifecycle state after the verdict (``evaluating``
    on a pass, ``screening_failed`` on a fail). ``accepted`` is ``True`` when the
    verdict was applied or was already in effect (idempotent re-report).
    """

    agent_id: Annotated[UUID, Field(description="Echoes the path-param id.")]
    status: Annotated[
        AgentStatus, Field(description="Lifecycle state after the verdict.")
    ]
    accepted: Annotated[
        bool, Field(description="``True`` when the verdict was applied.")
    ]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "550e8400-e29b-41d4-a716-446655440000",
                "status": "evaluating",
                "accepted": True,
            }
        }
    )
