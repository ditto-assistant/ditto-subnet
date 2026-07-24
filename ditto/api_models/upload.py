"""Wire shapes for the ``/upload/*`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from ditto.api_models.agent_status import AgentStatus

# SS58 addresses are 47-48 chars from the base58 alphabet (no 0, O, I, l).
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

# SHA256 hex = 64 lowercase hex chars.
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

# sr25519 signature = 64 bytes = 128 hex chars (case-insensitive accepted).
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"

# Substrate block hash = 0x + 64 hex chars (case-insensitive).
_BLOCK_HASH_PATTERN = r"^0x[0-9a-fA-F]{64}$"


class EvalPricingResponse(BaseModel):
    """Returned by ``GET /upload/eval-pricing``."""

    amount_rao: Annotated[int, Field(ge=1)]
    """TAO amount in rao (1 TAO = 1e9 rao) the miner must pay."""

    send_address: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """Ditto-controlled SS58 receive address for the upload fee."""


class UploadCheckRequest(BaseModel):
    """Body of ``POST /upload/check``.

    The signature is over the UTF-8 bytes of ``f"{hotkey}:{sha256}"``,
    produced by the hotkey's keypair (sr25519 by default).
    """

    hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """Submitting miner's SS58 hotkey."""

    sha256: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    """Lowercase hex of the tarball SHA-256 digest."""

    file_size_bytes: Annotated[int, Field(ge=1)]
    """Tarball size in bytes. Server caps at MAX_TARBALL_SIZE_BYTES."""

    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    """Hex sr25519 signature over ``f"{hotkey}:{sha256}"``."""


class UploadCheckResponse(BaseModel):
    """Returned by ``POST /upload/check``.

    Parallel ``error_codes`` + ``messages`` arrays let the miner CLI
    branch on numeric codes while still surfacing human-readable
    reasons in logs.
    """

    ok: bool
    """``True`` iff every server-side validation passed."""

    error_codes: list[int]
    """One entry per failed validation. Empty when ``ok``."""

    messages: list[str]
    """Parallel array of human-readable failure reasons. Empty when ``ok``."""

    retry_at: datetime | None = None
    """UTC timestamp when an owner coldkey blocked by cooldown may retry."""


class UploadAgentResponse(BaseModel):
    """Returned by ``POST /upload/agent`` on a successful upload.

    The endpoint's only positive output: the server-generated
    ``agent_id`` plus the lifecycle state the row was inserted at. The
    retrieval endpoints (next PR) expose anything else the miner CLI
    might want to poll for.
    """

    agent_id: UUID
    """Server-generated UUID identifying the inserted agent row. Use
    this to track screening + evaluation status via the retrieval
    endpoints."""

    version: Annotated[
        int,
        Field(
            ge=1,
            description="1-based submission version for this hotkey and agent name.",
        ),
    ]

    status: AgentStatus
    """Initial lifecycle state. Always ``uploaded`` immediately after a
    successful upload; the platform-operated screening service advances it
    shortly after."""
