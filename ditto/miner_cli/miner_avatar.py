"""Mint a signed miner profile-picture set or clear.

Signing a message does not move funds. The server-side builders live in
``apps/platform/ditto/api_server/miner_avatar.py``; these are a byte-for-byte
mirror of the payload strings.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime

    import bittensor

SET_DOMAIN: Final = "ditto-miner-avatar:v1"
CLEAR_DOMAIN: Final = "ditto-miner-avatar-clear:v1"

KeyKind = Literal["hotkey", "coldkey"]


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def set_message(
    *,
    netuid: int,
    miner_hotkey: str,
    content_sha256: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = _issued_stamp(issued_at)
    return (
        f"{SET_DOMAIN}:{netuid}:{miner_hotkey}:{content_sha256}:{nonce}:{issued}"
        f":{key_kind}:{signer}"
    ).encode()


def clear_message(
    *,
    netuid: int,
    miner_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = _issued_stamp(issued_at)
    return (
        f"{CLEAR_DOMAIN}:{netuid}:{miner_hotkey}:{nonce}:{issued}"
        f":{key_kind}:{signer}"
    ).encode()


def sign_payload(
    *, live_wallet: bittensor.Wallet, key_kind: KeyKind, payload: bytes
) -> str:
    keypair = live_wallet.hotkey if key_kind == "hotkey" else live_wallet.coldkey
    return keypair.sign(payload).hex()


def signer_address(*, live_wallet: bittensor.Wallet, key_kind: KeyKind) -> str:
    if key_kind == "hotkey":
        return str(live_wallet.hotkey.ss58_address)
    coldkey = getattr(live_wallet, "coldkey", None)
    if coldkey is not None:
        return str(coldkey.ss58_address)
    return str(live_wallet.coldkeypub.ss58_address)
