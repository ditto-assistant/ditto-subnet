"""Reviewer-facing wire shapes for owner-link attestations.

Backroom reads these while adjudicating a copy hold: "is this miner's claim
that the earlier submission was also theirs actually proven, or is it a story?"
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AdminOwnerAttestation(BaseModel):
    """One attestation row as a reviewer sees it."""

    attestation_id: UUID
    netuid: int

    hotkey_lo: str
    hotkey_hi: str

    counterparty: str
    """The *other* hotkey, relative to the one queried.

    The link is symmetric, so there is no direction to report -- this is simply
    who the queried hotkey is linked to.
    """

    evidence_grade: Literal["coldkey-coldkey", "mixed", "hotkey-hotkey"]
    """Strength of the two proofs. Context for the reviewer; it does not gate
    the exemption, and screening treats all three identically."""

    lo_key_kind: Literal["hotkey", "coldkey"]
    lo_signer: str
    hi_key_kind: Literal["hotkey", "coldkey"]
    hi_signer: str

    nonce: UUID
    issued_at: datetime
    created_at: datetime

    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revoked_reason: str | None = None

    active: bool
    """Whether the link counts right now. Revoked rows are still returned:
    "was this link live when that submission was screened" is the question a
    dispute actually turns on."""


class AdminLinkedHotkey(BaseModel):
    """A hotkey proven to be the same operator as the one queried."""

    hotkey: str
    attestation_id: UUID
    """The link that proves it, so a reviewer can go straight to the evidence."""

    evidence_grade: Literal["coldkey-coldkey", "mixed", "hotkey-hotkey"]


class AdminOwnerAttestationsResponse(BaseModel):
    """Returned by ``GET /admin/owner-attestations/{hotkey}``."""

    hotkey: str
    netuid: int

    attestations: list[AdminOwnerAttestation]
    """Every link naming this hotkey on either side, oldest first."""

    linked_hotkeys: list[AdminLinkedHotkey]
    """Currently-active links only. Direct links; the relation is not
    transitive, so a hotkey linked to a hotkey linked to this one is absent."""

    linkage_basis: Literal["signed_owner_attestation"] = "signed_owner_attestation"
    """Distinguishes this from the payment-record inference exposed by
    ``GET /admin/miner-owners/{identifier}``. A signature proves control of a
    key; a shared coldkey only suggests a shared payer. Where the two disagree,
    this one is the stronger evidence."""

    scope_caveat: str = (
        "An owner-link attestation exempts each linked hotkey from "
        "plagiarism screening against the other's submissions, including "
        "byte-identical and repacked generations, and clears existing pending "
        "copy holds for the direct pair. It is not an input to emission-slot "
        "allocation, "
        "which remains partitioned by payment-time coldkey. The evidence grade "
        "is reviewer context and does not gate the exemption."
    )
    """Stated on the wire so no downstream surface has to invent the wording."""


class AdminAttestationRevokeRequest(BaseModel):
    """Body of ``POST /admin/owner-attestations/{attestation_id}/revoke``."""

    reason: Annotated[str, Field(min_length=8)]
    """Why the link is being withdrawn. Recorded on the row for audit."""
