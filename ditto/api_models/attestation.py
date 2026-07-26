"""Wire shapes for the ``/attestations/*`` endpoints.

A hotkey-rotation attestation is the self-serve replacement for a Discord
conversation about "I lost my old key, that earlier submission was also mine".
It carries two sr25519 signatures over the same signed tuple: the old hotkey
attests, the new hotkey accepts.

These models are a byte-identical copy of ``ditto-platform``'s
``ditto/api_models/attestation.py``, which is the source of truth; any change
there must land here too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# SS58 addresses are 47-48 chars from the base58 alphabet (no 0, O, I, l).
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"

# sr25519 signature = 64 bytes = 128 hex chars (case-insensitive accepted).
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"


class HotkeyAttestationRequest(BaseModel):
    """Body of ``POST /attestations/hotkey-rotation``.

    ``attestation_signature`` is over the UTF-8 bytes of::

        ditto-hotkey-attestation:v1:{netuid}:{old_hotkey}:{new_hotkey}:{nonce}:{issued_at}

    and ``acceptance_signature`` over the same tuple with the
    ``ditto-hotkey-attestation-accept:v1`` tag. ``issued_at`` is serialised as
    an ISO-8601 UTC timestamp with microsecond precision. Both builders live in
    :mod:`ditto.api_server.attestation` and are mirrored in the miner CLI.
    """

    netuid: Annotated[int, Field(ge=0)]
    """Subnet the attestation was minted for. Must match this deployment."""

    old_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """Predecessor hotkey; the signer of ``attestation_signature``."""

    new_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """Successor hotkey; the signer of ``acceptance_signature``."""

    nonce: UUID
    """Single-use value bound into both payloads. Replay guard."""

    issued_at: datetime
    """Mint time, signed. Must be inside the server's acceptance window."""

    attestation_signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    """Hex sr25519 signature by ``old_hotkey``."""

    acceptance_signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    """Hex sr25519 signature by ``new_hotkey``."""


class HotkeyAttestationResponse(BaseModel):
    """Returned by ``POST /attestations/hotkey-rotation`` on success."""

    attestation_id: UUID
    """Surrogate id of the recorded link."""

    netuid: int
    old_hotkey: str
    new_hotkey: str

    created_at: datetime
    """When the link became active. Screening from this point on sees it."""

    scope: Literal["plagiarism-screening-only"] = "plagiarism-screening-only"
    """What the link does. Stated on the wire so nobody has to infer it.

    It exempts ``new_hotkey`` from the near-duplicate copy-screening rules when
    compared against ``old_hotkey``'s earlier submissions. It does **not**
    exempt byte-identical or repacked resubmission, and it does **not** touch
    emission-slot allocation.
    """

    grants_additional_emission_slot: Literal[False] = False
    """Always false. Emission positions are partitioned by payment-time coldkey
    and this link is not an input to that expression."""
