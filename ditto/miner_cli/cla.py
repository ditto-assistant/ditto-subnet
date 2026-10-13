"""Mint and verify a signed SN118 Contributor License Agreement (CLA) payload.

Byte-for-byte mirror of 'apps/platform/ditto/api_server/cla.py'.
Follows the signed-action conventions in 'ditto/miner_cli/name_claim.py'.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from typing import TYPE_CHECKING, Final
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime
    import bittensor

CLA_DOMAIN: Final = "ditto-cla:v1"


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def compute_cla_sha256(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def verify_cla_digest(*, content: str | bytes, expected_sha256: str) -> str:
    actual = compute_cla_sha256(content)
    if actual.lower() != expected_sha256.lower():
        raise ValueError(
            f"CLA SHA-256 mismatch: expected {expected_sha256}, computed {actual}. Refusing to sign."
        )
    return actual


def cla_message(
    *,
    netuid: int,
    cla_version: int,
    cla_sha256: str,
    hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> bytes:
    """Construct canonical bytes:
    ditto-cla:v1:<netuid>:<cla_version>:<cla_sha256>:<hotkey>:<nonce>:<issued_at>
    """
    issued = _issued_stamp(issued_at)
    return (
        f"{CLA_DOMAIN}:{netuid}:{cla_version}:{cla_sha256.lower()}:"
        f"{hotkey}:{nonce}:{issued}"
    ).encode("utf-8")


def sign_cla_payload(*, live_wallet: bittensor.Wallet, payload: bytes) -> str:
    """Sign CLA payload with the miner hotkey (sr25519) and return lowercase hex."""
    return live_wallet.hotkey.sign(payload).hex()
