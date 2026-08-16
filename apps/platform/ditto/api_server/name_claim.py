"""Signed miner handle claims: what is signed, and how a name is reserved.

Anyone can upload an agent named ``Jupiter-ditto-v10``. The rightful owner of
the Jupiter handle has no cryptographic way to say "that name is mine" -- the
only remedy today is a Discord conversation. This module is the replacement.

A miner signs a claim over a **name stem**. Other miners who already have a
scored payment-owner family -- entrenched operators, not a fresh hotkey --
endorse that claim. Once enough distinct families have signed, the stem is
reserved to the claimant's owner family: new uploads that collide are
rejected, and existing collisions are marked ``disputed`` on the public
board.

What is a stem
--------------
``Jupiter-ditto-v10``, ``jupiter``, and ``JUPITER_v2`` are the same handle.
Version tokens, ``ditto`` / ``sn118`` / ``subnet`` / ``miner`` filler, and
separator differences collapse away. The stem is what is reserved, so a
copycat cannot dodge the reservation by appending ``-v11``.

What is bound into each signature
---------------------------------
- a **domain tag + version** so a claim signature cannot be replayed into
  upload, owner-link, or validator lanes;
- the **netuid**, so a claim minted on another deployment cannot land here;
- the **stem**, the **signer identity**, and (for endorsements / withdrawals)
  the **claim id**, so a captured signature cannot be retargeted;
- a **nonce** (unique, replay-guarded) and **issued_at** (freshness window).

Endorsers must be entrenched
----------------------------
An endorsement is only accepted from a payment-owner family that already has
a full-benchmark scored submission whose earliest upload is older than
:data:`ENTRENCHMENT_AGE`. One endorsement per owner family. The claimant's
own family cannot endorse itself. Brand-new miners cannot manufacture a
quorum.

The claim does not move emissions. It only governs the public name.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from ditto.api_server.attestation import (
    MAX_ATTESTATION_AGE,
    MAX_ISSUED_AT_SKEW,
    verify_signature,
)
from ditto.db.queries.scores import emission_owner

CLAIM_DOMAIN: Final = "ditto-name-claim:v1"
ENDORSE_DOMAIN: Final = "ditto-name-endorse:v1"
WITHDRAW_DOMAIN: Final = "ditto-name-withdraw:v1"

KeyKind = Literal["hotkey", "coldkey"]
ClaimStatus = Literal["pending", "upheld", "withdrawn"]
HandleStatus = Literal["reserved", "disputed", "pending"]

ENDORSEMENT_THRESHOLD: Final = 3
"""Distinct entrenched owner families required to uphold a claim."""

ENTRENCHMENT_AGE: Final = timedelta(days=7)
"""How long a scored family must have existed before it can endorse."""

MIN_STEM_LENGTH: Final = 3
MAX_STEM_LENGTH: Final = 64

_FILLER_TOKENS: Final = frozenset({"ditto", "sn118", "subnet", "miner", "agent"})
_VERSION_TOKEN: Final = re.compile(r"^v?\d+$")
_SEPARATORS: Final = re.compile(r"[_.\s]+")
_NON_STEM: Final = re.compile(r"[^a-z0-9-]+")
_DASHES: Final = re.compile(r"-{2,}")


class NameClaimRejected(Exception):
    """A name-claim, endorsement, or withdrawal failed verification or policy."""


def expected_netuid() -> int:
    """Netuid this deployment accepts name claims for."""
    return int(os.environ.get("NETUID", "118"))


def normalize_name_stem(name: str) -> str:
    """Collapse a display name to the reserved handle.

    Lowercases, treats ``_`` / ``.`` / whitespace as dashes, drops characters
    outside ``[a-z0-9-]``, then removes filler and version tokens. Empty
    after that means the name has no reservable handle.
    """
    folded = unicodedata.normalize("NFKC", name).strip().lower()
    folded = _SEPARATORS.sub("-", folded)
    folded = _NON_STEM.sub("", folded)
    folded = _DASHES.sub("-", folded).strip("-")
    tokens = [
        token
        for token in folded.split("-")
        if token
        and token not in _FILLER_TOKENS
        and _VERSION_TOKEN.fullmatch(token) is None
    ]
    return "-".join(tokens)


def require_name_stem(name: str) -> str:
    """Normalize ``name`` and reject stems that cannot be reserved."""
    stem = normalize_name_stem(name)
    if not (MIN_STEM_LENGTH <= len(stem) <= MAX_STEM_LENGTH):
        raise NameClaimRejected(
            f"name {name!r} does not yield a reservable handle "
            f"({MIN_STEM_LENGTH}-{MAX_STEM_LENGTH} characters after "
            "stripping versions and filler)"
        )
    return stem


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def claim_message(
    *,
    netuid: int,
    name_stem: str,
    claimant_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes a claimant signs."""
    issued = _issued_stamp(issued_at)
    return (
        f"{CLAIM_DOMAIN}:{netuid}:{name_stem}:{claimant_hotkey}:{nonce}:{issued}"
        f":{key_kind}:{signer}"
    ).encode()


def endorse_message(
    *,
    netuid: int,
    claim_id: UUID,
    name_stem: str,
    endorser_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes an endorser signs."""
    issued = _issued_stamp(issued_at)
    return (
        f"{ENDORSE_DOMAIN}:{netuid}:{claim_id}:{name_stem}:{endorser_hotkey}"
        f":{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def withdraw_message(
    *,
    netuid: int,
    claim_id: UUID,
    name_stem: str,
    claimant_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Exact UTF-8 bytes a claimant signs to release a stem."""
    issued = _issued_stamp(issued_at)
    return (
        f"{WITHDRAW_DOMAIN}:{netuid}:{claim_id}:{name_stem}:{claimant_hotkey}"
        f":{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def check_freshness(*, issued_at: datetime, now: datetime) -> None:
    """Reject signatures minted too far in the past or future."""
    issued = issued_at.astimezone(UTC)
    if issued > now + MAX_ISSUED_AT_SKEW:
        raise NameClaimRejected(
            "name-claim issued_at is in the future beyond the allowed skew"
        )
    if issued < now - MAX_ATTESTATION_AGE:
        raise NameClaimRejected(
            "name-claim signature has expired; mint a fresh one and submit it again"
        )


def verify_signed_action(
    *,
    payload: bytes,
    hotkey: str,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    bound_coldkey: str | None,
) -> None:
    """Verify one signed claim / endorsement / withdrawal, or raise.

    ``hotkey`` proofs must be signed by that hotkey. ``coldkey`` proofs must
    be signed by the payment-bound coldkey the platform already knows for
    that hotkey -- never a coldkey the request itself asserts.
    """
    if key_kind == "hotkey":
        if signer != hotkey:
            raise NameClaimRejected(
                "hotkey proof signer must be the named hotkey itself"
            )
    elif key_kind == "coldkey":
        if bound_coldkey is None:
            raise NameClaimRejected(
                "coldkey proof requires a payment record binding that hotkey "
                "to a coldkey; sign with the hotkey itself instead"
            )
        if signer != bound_coldkey:
            raise NameClaimRejected(
                "coldkey proof signer is not the payment-bound coldkey for this hotkey"
            )
    else:
        raise NameClaimRejected(f"unknown key_kind {key_kind!r}")

    if not verify_signature(signer=signer, payload=payload, signature_hex=signature):
        raise NameClaimRejected("signature did not verify")


def owner_root_for(*, miner_hotkey: str, miner_coldkey: str | None) -> str:
    """Payment-owner identity used to group families and endorsements."""
    return emission_owner(miner_hotkey=miner_hotkey, miner_coldkey=miner_coldkey)


def handle_status_for(
    *,
    agent_name: str,
    owner_root: str | None,
    claims: dict[str, tuple[ClaimStatus, str]],
) -> HandleStatus | None:
    """Classify one leaderboard name against the active claim set.

    ``claims`` maps stem -> ``(status, claimant_owner_root)``.
    """
    stem = normalize_name_stem(agent_name)
    claim = claims.get(stem)
    if claim is None:
        return None
    status, claimant_root = claim
    if status == "pending":
        return "pending"
    if status != "upheld":
        return None
    if owner_root is not None and owner_root == claimant_root:
        return "reserved"
    return "disputed"
