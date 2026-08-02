"""Wire shapes for the ``/attestations/*`` endpoints.

An owner-link attestation is the self-serve replacement for a Discord
conversation about "I rotated keys, that earlier submission was also mine".
Two hotkeys are declared to be the same operator and **both** endpoints sign,
each with either its own hotkey or the coldkey bound to it.

``ditto-subnet`` keeps a byte-identical copy of these models; any change here
must land in both repositories.
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


class OwnerLinkProof(BaseModel):
    """One endpoint's proof that it consents to the link.

    The signature is over the UTF-8 bytes of::

        ditto-owner-link:v1:{netuid}:{hotkey_lo}:{hotkey_hi}:{nonce}:{issued_at}
        :{side}:{key_kind}:{signer}

    (one line, no wrapping) where ``hotkey_lo``/``hotkey_hi`` are the two
    hotkeys sorted, ``side`` is ``lo`` or ``hi``, and ``issued_at`` is an
    ISO-8601 UTC timestamp with microsecond precision. The builder lives in
    :mod:`ditto.api_server.attestation` and is mirrored in the miner CLI.
    """

    key_kind: Literal["hotkey", "coldkey"]
    """Which key proved this half.

    ``hotkey``: ``signer`` must be this side's hotkey itself.
    ``coldkey``: ``signer`` must be the coldkey on that hotkey's most recent
    payment record -- a binding the platform knows independently of this
    request.
    """

    signer: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """SS58 that produced ``signature``. Signed, so it cannot be relabelled."""

    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    """Hex sr25519 signature over this side's payload."""


class OwnerLinkRequest(BaseModel):
    """Body of ``POST /attestations/owner-link``.

    ``hotkey_a`` / ``hotkey_b`` may be given in any order; the server sorts
    them into canonical ``lo``/``hi`` order, and each proof must correspond to
    the side its hotkey lands on. The link is symmetric, so there is no
    "from"/"to".
    """

    netuid: Annotated[int, Field(ge=0)]
    """Subnet the attestation was minted for. Must match this deployment."""

    hotkey_a: Annotated[str, Field(pattern=_SS58_PATTERN)]
    hotkey_b: Annotated[str, Field(pattern=_SS58_PATTERN)]

    nonce: UUID
    """Single-use value bound into both payloads. Replay guard."""

    issued_at: datetime
    """Mint time, signed. Must be inside the server's acceptance window."""

    proof_a: OwnerLinkProof
    """Proof from the holder of ``hotkey_a``."""

    proof_b: OwnerLinkProof
    """Proof from the holder of ``hotkey_b``."""


class OwnerLinkResponse(BaseModel):
    """Returned by ``POST /attestations/owner-link`` on success."""

    attestation_id: UUID
    """Surrogate id of the recorded link."""

    netuid: int
    hotkey_lo: str
    hotkey_hi: str

    evidence_grade: Literal["coldkey-coldkey", "mixed", "hotkey-hotkey"]
    """Relative strength of the two proofs.

    **Reviewer context only.** All three grades establish the link identically;
    nothing in screening branches on this value.
    """

    created_at: datetime
    """When the link became active."""

    cleared_copy_review_count: Annotated[int, Field(ge=0)] = 0
    """Pending direct-pair copy holds automatically cleared by this link."""

    scope: Literal["plagiarism-screening-only"] = "plagiarism-screening-only"
    """What the link does. Stated on the wire so nobody has to infer it.

    It exempts each linked hotkey from copy screening against the other's
    submissions, including byte-identical and repacked generations, and clears
    existing pending copy holds for this direct pair. It does **not** touch
    emission-slot allocation.
    """

    grants_additional_emission_slot: Literal[False] = False
    """Always false. Emission positions are partitioned by payment-time coldkey
    and this link is not an input to that expression."""
