"""Wire shapes for the ``/upload/*`` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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

    allow_identical_rescore: bool = False
    """Explicitly permit buying another seed for byte-identical source."""

    reserve_submission_slot: bool = False
    """Reserve this owner's currently eligible slot before payment.

    Older clients may leave this false. New miner clients set it true and refuse
    to pay unless the response contains an admission token.
    """

    payment_block_hash: Annotated[str, Field(pattern=_BLOCK_HASH_PATTERN)] | None = None
    payment_block_number: Annotated[int, Field(ge=1)] | None = None
    payment_extrinsic_index: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def finalized_payment_proof_is_all_or_none(self) -> UploadCheckRequest:
        """A finalized proof authorizes reuse; partial identity never can."""
        supplied = (
            self.payment_block_hash is not None,
            self.payment_block_number is not None,
            self.payment_extrinsic_index is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError("finalized payment proof fields must be supplied together")
        return self


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

    payment_required: bool = True
    """False when an existing same-owner artifact makes payment unnecessary."""

    identical_agent_id: UUID | None = None
    """Existing same-owner submission when identical bytes were detected."""

    identical_agent_status: AgentStatus | None = None
    """Current lifecycle state of :attr:`identical_agent_id`."""

    retry_at: datetime | None = None
    """UTC timestamp when an owner coldkey blocked by cooldown may retry."""

    admission_token: UUID | None = None
    """Opaque reservation consumed by the matching upload after payment."""

    admission_expires_at: datetime | None = None
    """UTC expiry of :attr:`admission_token`."""

    cooldown_seconds: Annotated[int, Field(ge=60, le=86400)] | None = None
    """Platform-controlled owner cooldown used for this decision."""

    payment_amount_rao: Annotated[int, Field(ge=1)] | None = None
    """Fee bound to this admission; use this value instead of requoting."""

    payment_send_address: Annotated[str, Field(pattern=_SS58_PATTERN)] | None = None
    """Destination bound to the payment instructions for this admission."""


class UploadAgentResponse(BaseModel):
    """Returned by ``POST /upload/agent`` on a successful upload.

    A new artifact returns its server-generated identity. An accidental
    byte-identical upload returns the existing identity plus a reusable payment
    credit disposition, so the miner can fund different source without paying
    again.
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
    """Lifecycle state of the returned agent."""

    payment_disposition: Literal["consumed", "credit_consumed", "reusable_credit"] = (
        "consumed"
    )
    """Whether this proof funded the returned agent or remains reusable."""

    credit_for_agent_id: UUID | None = None
    """Existing identical submission that caused a reusable credit."""
