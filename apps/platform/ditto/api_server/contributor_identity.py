"""Proof verification against finalized chain ownership, without UID admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ditto.api_models.contributor_identity import KeyKind
from ditto.api_server.attestation import (
    MAX_ATTESTATION_AGE,
    MAX_ISSUED_AT_SKEW,
    verify_signature,
)
from ditto.chain import ExtrinsicNotFoundError

if TYPE_CHECKING:
    from ditto.chain import ChainClient


class ContributorProofRejected(Exception):
    pass


@dataclass(frozen=True)
class FinalizedOwner:
    coldkey: str
    block: int
    block_hash: str


def check_contributor_proof(
    *,
    payload: bytes,
    hotkey: str,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    issued_at: datetime,
    now: datetime,
) -> None:
    if issued_at.tzinfo is None:
        raise ContributorProofRejected("issued_at must include a timezone")
    if issued_at > now + MAX_ISSUED_AT_SKEW:
        raise ContributorProofRejected("issued_at is too far in the future")
    if issued_at < now - MAX_ATTESTATION_AGE:
        raise ContributorProofRejected("signature has expired; sign again")
    if key_kind == "hotkey" and signer != hotkey:
        raise ContributorProofRejected("hotkey proof signer must be the named hotkey")
    if not verify_signature(signer=signer, payload=payload, signature_hex=signature):
        raise ContributorProofRejected("signature did not verify")


async def finalized_owner(chain: ChainClient, hotkey: str) -> FinalizedOwner:
    block = await chain.get_finalized_block()
    try:
        coldkey = await chain.get_coldkey_for_hotkey(hotkey, block.hash)
    except ExtrinsicNotFoundError as exc:
        # At this lookup boundary the client uses this error only for an
        # empty Owner entry. get_finalized_block errors remain retryable.
        raise ContributorProofRejected(
            "hotkey has no finalized Owner entry; register ownership on "
            "chain first (an SN118 mining UID is not required)"
        ) from exc
    # The zero AccountId is the storage default for a nonexistent owner.
    import bittensor

    try:
        key = bittensor.Keypair(ss58_address=coldkey)
        public_key = key.public_key
        if public_key is None:
            raise ValueError("missing owner")
        decoded = (
            bytes.fromhex(public_key.removeprefix("0x"))
            if isinstance(public_key, str)
            else bytes(public_key)
        )
        if not decoded or decoded == bytes(32):
            raise ValueError("missing owner")
    except (ValueError, TypeError) as error:
        raise ContributorProofRejected(
            "Owner(hotkey) is not a valid finalized coldkey"
        ) from error
    return FinalizedOwner(coldkey=coldkey, block=block.number, block_hash=block.hash)
