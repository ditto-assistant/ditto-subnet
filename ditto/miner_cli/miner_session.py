"""Mint a signed miner login approval.

Byte-for-byte mirror of ``apps/platform/ditto/api_server/miner_session.py``
payload construction. Signing does not move funds.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime

    import bittensor

LOGIN_DOMAIN: Final = "ditto-miner-login:v1"
KNOWN_SCOPES: Final[tuple[str, ...]] = (
    "read",
    "profile",
    "download",
    "upload",
    "handle",
    "challenges",
)
KeyKind = Literal["hotkey", "coldkey"]


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def normalize_user_code(raw: str) -> str:
    compact = "".join(ch for ch in raw.upper() if ch.isalnum())
    if len(compact) != 8:
        raise ValueError("user code must look like ABCD-EFGH")
    return f"{compact[:4]}-{compact[4:]}"


def login_message(
    *,
    netuid: int,
    miner_hotkey: str,
    user_code: str,
    grant_id: UUID,
    ttl_seconds: int,
    scopes: str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
    oauth_client_id: str | None = None,
    redirect_uri: str | None = None,
) -> bytes:
    issued = _issued_stamp(issued_at)
    code = normalize_user_code(user_code)
    parts = {part.strip() for part in scopes.split(",") if part.strip()}
    unknown = sorted(parts - set(KNOWN_SCOPES))
    if unknown:
        raise ValueError("unknown miner session scope: " + ", ".join(unknown))
    if not parts:
        raise ValueError("at least one miner session scope is required")
    ordered = ",".join(scope for scope in KNOWN_SCOPES if scope in parts)
    client = oauth_client_id or "-"
    redirect = redirect_uri or "-"
    return (
        f"{LOGIN_DOMAIN}:{netuid}:{miner_hotkey}:{code}:{grant_id}:{ttl_seconds}"
        f":{ordered}:{nonce}:{issued}:{key_kind}:{signer}:{client}:{redirect}"
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
