"""Byte-for-byte message builders for 'ditto link-github' and 'ditto link-github unlink'.

Mirrors 'apps/platform/ditto/api_server/github_link.py'.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime
    import bittensor

GITHUB_LINK_DOMAIN: Final = "ditto-github-link:v1"
GITHUB_UNLINK_DOMAIN: Final = "ditto-github-unlink:v1"
KeyKind = Literal["hotkey", "coldkey"]


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def github_link_message(
    *,
    netuid: int,
    hotkey: str,
    github_user_id: int,
    github_login: str,
    link_attempt_id: UUID,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Construct canonical bytes:
    ditto-github-link:v1:<netuid>:<hotkey>:<github_user_id>:<github_login>:<link_attempt_id>:<nonce>:<issued_at>:<key_kind>:<signer>
    """
    issued = _issued_stamp(issued_at)
    return (
        f"{GITHUB_LINK_DOMAIN}:{netuid}:{hotkey}:{github_user_id}:{github_login}:"
        f"{link_attempt_id}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode("utf-8")


def github_unlink_message(
    *,
    netuid: int,
    hotkey: str,
    github_user_id: int,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    issued = _issued_stamp(issued_at)
    return (
        f"{GITHUB_UNLINK_DOMAIN}:{netuid}:{hotkey}:{github_user_id}:"
        f"{nonce}:{issued}:{key_kind}:{signer}"
    ).encode("utf-8")


def sign_payload(*, live_wallet: bittensor.Wallet, key_kind: KeyKind, payload: bytes) -> str:
    keypair = live_wallet.hotkey if key_kind == "hotkey" else live_wallet.coldkey
    return keypair.sign(payload).hex()
