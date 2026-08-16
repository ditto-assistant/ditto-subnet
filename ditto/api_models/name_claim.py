"""Wire shapes for miner handle claims and endorsements.

A claim is the self-serve replacement for a Discord conversation about
"that agent stole my name". The claimant signs, entrenched miners with
scored agent families endorse, and an upheld stem is reserved to the
claimant's owner family.

These models are a byte-identical copy of ``ditto-platform``'s
``ditto/api_models/name_claim.py``, which is the source of truth; any change
there must land here too.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_STEM_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class NameClaimProof(BaseModel):
    """One sr25519 proof of a name-claim action.

    The signed bytes are documented on the request that carries the proof
    (:class:`NameClaimRequest`, :class:`NameEndorseRequest`,
    :class:`NameWithdrawRequest`).
    """

    model_config = ConfigDict(extra="ignore")

    key_kind: Literal["hotkey", "coldkey"]
    """Which key produced ``signature``.

    ``hotkey``: ``signer`` must be the named hotkey.
    ``coldkey``: ``signer`` must be the payment-bound coldkey for that hotkey.
    """

    signer: Annotated[str, Field(pattern=_SS58_PATTERN)]
    """SS58 that produced ``signature``. Signed, so it cannot be relabelled."""

    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    """Hex sr25519 signature over the action payload."""


class NameClaimRequest(BaseModel):
    """Body of ``POST /name-claims``.

    Signature is over the UTF-8 bytes of::

        ditto-name-claim:v1:{netuid}:{name_stem}:{claimant_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}

    ``name_stem`` is the normalized handle. Clients may send a display name
    in ``name`` instead; the server normalizes it and the signed stem must
    match that result.
    """

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    """Display name or already-normalized stem the claimant is reserving."""

    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class NameEndorseRequest(BaseModel):
    """Body of ``POST /name-claims/{claim_id}/endorsements``.

    Signature is over::

        ditto-name-endorse:v1:{netuid}:{claim_id}:{name_stem}:{endorser_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}
    """

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    name_stem: Annotated[str, Field(pattern=_STEM_PATTERN, min_length=3, max_length=64)]
    endorser_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class NameWithdrawRequest(BaseModel):
    """Body of ``POST /name-claims/{claim_id}/withdraw``.

    Signature is over::

        ditto-name-withdraw:v1:{netuid}:{claim_id}:{name_stem}:{claimant_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}
    """

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: NameClaimProof


class NameClaimEndorsementView(BaseModel):
    """One recorded endorsement, minus the raw signature."""

    model_config = ConfigDict(extra="ignore")

    endorsement_id: UUID
    endorser_hotkey: str
    created_at: datetime


class NameClaimResponse(BaseModel):
    """Returned by claim / endorse / withdraw / get."""

    model_config = ConfigDict(extra="ignore")

    claim_id: UUID
    netuid: int
    name_stem: str
    claimant_hotkey: str
    status: Literal["pending", "upheld", "withdrawn"]
    endorsement_count: Annotated[int, Field(ge=0)]
    endorsement_threshold: Annotated[int, Field(ge=1)]
    endorsements: list[NameClaimEndorsementView] = Field(default_factory=list)
    created_at: datetime
    upheld_at: datetime | None = None
    withdrawn_at: datetime | None = None
    scope: Literal["public-handle-only"] = "public-handle-only"
    """What the claim does. Stated on the wire so nobody has to infer it.

    An upheld claim reserves the stem to the claimant's payment-owner family
    for new uploads and marks colliding leaderboard names as disputed. It
    does **not** touch emission-slot allocation, screening, or scores.
    """


class NameClaimListResponse(BaseModel):
    """Returned by ``GET /name-claims`` and ``GET /public/name-claims``."""

    model_config = ConfigDict(extra="ignore")

    generated_at: datetime
    endorsement_threshold: Annotated[int, Field(ge=1)]
    claims: list[NameClaimResponse]


class PublicNameHandle(BaseModel):
    """Optional handle annotation on a public leaderboard entry."""

    model_config = ConfigDict(extra="ignore")

    stem: Annotated[str, Field(min_length=1, max_length=64)]
    status: Literal["reserved", "disputed", "pending"]
    claim_id: UUID
