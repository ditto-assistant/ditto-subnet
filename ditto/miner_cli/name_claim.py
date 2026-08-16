"""Mint a signed handle claim, endorsement, or withdrawal.

Signing a message does not move funds. Every function here hands a byte
string to a keypair's ``sign`` method. The server-side builders live in
``ditto/api_server/name_claim.py``; these are a byte-for-byte mirror.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime

    import bittensor

CLAIM_DOMAIN: Final = "ditto-name-claim:v1"
ENDORSE_DOMAIN: Final = "ditto-name-endorse:v1"
WITHDRAW_DOMAIN: Final = "ditto-name-withdraw:v1"

KeyKind = Literal["hotkey", "coldkey"]


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
    issued = _issued_stamp(issued_at)
    return (
        f"{WITHDRAW_DOMAIN}:{netuid}:{claim_id}:{name_stem}:{claimant_hotkey}"
        f":{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def sign_payload(
    *, live_wallet: bittensor.Wallet, key_kind: KeyKind, payload: bytes
) -> str:
    """Return lowercase hex sr25519 signature from the chosen key."""
    keypair = live_wallet.hotkey if key_kind == "hotkey" else live_wallet.coldkey
    return keypair.sign(payload).hex()


def signer_address(*, live_wallet: bittensor.Wallet, key_kind: KeyKind) -> str:
    if key_kind == "hotkey":
        return str(live_wallet.hotkey.ss58_address)
    coldkey = getattr(live_wallet, "coldkey", None)
    if coldkey is not None:
        return str(coldkey.ss58_address)
    return str(live_wallet.coldkeypub.ss58_address)
