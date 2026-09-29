"""Sign the domain-separated, short-lived upload payload with the hotkey.

The server verifies the same v2 payload in its upload endpoints. Any drift in
the payload format on either side breaks every upload.

This module exists as its own seam so signing has one canonical
implementation that is trivial to unit-test (round-trip verify against
``bittensor.Keypair.verify``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ditto.miner_cli.models import WalletHandle

if TYPE_CHECKING:
    import bittensor


def build_upload_payload(
    *,
    hotkey_ss58: str,
    sha256_hex: str,
    signature_timestamp: int,
    signature_nonce: UUID,
) -> bytes:
    """Return the exact UTF-8 bytes the CLI signs + the server verifies.

    Kept as a separate function so tests pin the wire format without
    pulling in the full signing path.
    """
    return (
        f"ditto-upload-v2:{hotkey_ss58}:{sha256_hex}:"
        f"{signature_timestamp}:{signature_nonce}"
    ).encode("ascii")


def sign_upload_payload(
    *,
    handle: WalletHandle,
    live_wallet: bittensor.Wallet,
    sha256_hex: str,
    signature_timestamp: int,
    signature_nonce: UUID,
) -> str:
    """Sign one upload request with the hotkey, return hex.

    Args:
        handle: Frozen identifying record for the loaded wallet. The
            ``hotkey_ss58`` field is the authoritative source for the
            payload's first field.
        live_wallet: Live bittensor wallet object holding the hotkey
            keypair. Passed separately from ``handle`` to keep
            :class:`WalletHandle` frozen-safe.
        sha256_hex: Lowercase hex SHA-256 of the tarball.
        signature_timestamp: Current Unix timestamp in seconds.
        signature_nonce: Fresh UUID4 for this request.

    Returns:
        The 128-hex sr25519 signature, lowercase. Server validates the
        format with ``_SIGNATURE_HEX_PATTERN`` in
        :mod:`ditto.api_models.upload`.
    """
    payload = build_upload_payload(
        hotkey_ss58=handle.hotkey_ss58,
        sha256_hex=sha256_hex,
        signature_timestamp=signature_timestamp,
        signature_nonce=signature_nonce,
    )
    signature_bytes = live_wallet.hotkey.sign(payload)
    return signature_bytes.hex()
