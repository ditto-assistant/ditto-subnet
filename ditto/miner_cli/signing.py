"""Sign the upload payload with the loaded hotkey.

The CLI signs ``f"{hotkey}:{sha256}"`` using sr25519 via the bittensor
SDK; the server verifies the same payload at
``ditto/api_server/endpoints/upload.py:128, 198``. Any drift in the
payload format on either side breaks every upload.

This module exists as its own seam so signing has one canonical
implementation that is trivial to unit-test (round-trip verify against
``bittensor.Keypair.verify``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ditto.miner_cli.models import WalletHandle

if TYPE_CHECKING:
    import bittensor


def build_upload_payload(*, hotkey_ss58: str, sha256_hex: str) -> bytes:
    """Return the exact UTF-8 bytes the CLI signs + the server verifies.

    Kept as a separate function so tests pin the wire format without
    pulling in the full signing path.
    """
    return f"{hotkey_ss58}:{sha256_hex}".encode()


def sign_upload_payload(
    *,
    handle: WalletHandle,
    live_wallet: bittensor.Wallet,
    sha256_hex: str,
) -> str:
    """Sign ``f"{hotkey}:{sha256}"`` with the hotkey, return hex.

    Args:
        handle: Frozen identifying record for the loaded wallet. The
            ``hotkey_ss58`` field is the authoritative source for the
            payload's first field.
        live_wallet: Live bittensor wallet object holding the hotkey
            keypair. Passed separately from ``handle`` to keep
            :class:`WalletHandle` frozen-safe.
        sha256_hex: Lowercase hex SHA-256 of the tarball.

    Returns:
        The 128-hex sr25519 signature, lowercase. Server validates the
        format with ``_SIGNATURE_HEX_PATTERN`` in
        :mod:`ditto.api_models.upload`.
    """
    payload = build_upload_payload(
        hotkey_ss58=handle.hotkey_ss58, sha256_hex=sha256_hex
    )
    signature_bytes = live_wallet.hotkey.sign(payload)
    return signature_bytes.hex()


def build_harness_logs_payload(
    *, hotkey_ss58: str, agent_id: str, requested_at: str
) -> bytes:
    """Return the exact UTF-8 bytes ``ditto logs`` signs + the server verifies.

    Server side is
    ``ditto/api_server/endpoints/miner_logs.py:build_harness_logs_payload``;
    ``test_harness_logs_payload_matches_platform`` pins the two byte-for-byte.
    Two copies rather than a shared import for the same reason
    :func:`build_upload_payload` has one: the CLI ships independently of the
    platform and must not depend on it.

    ``agent_id`` is inside the signed bytes deliberately. Signing only the hotkey
    would let a captured signature be re-pointed at any agent the same miner
    owns; binding the pair means one signature authorizes exactly one lookup.

    ``requested_at`` arrives pre-formatted so both ends serialize the timestamp
    exactly once, in one place -- a signature over a timestamp is only
    verifiable if both sides agree on its spelling to the microsecond.
    """
    return f"ditto-harness-logs:v1:{hotkey_ss58}:{agent_id}:{requested_at}".encode()


def sign_harness_logs_request(
    *,
    handle: WalletHandle,
    live_wallet: bittensor.Wallet,
    agent_id: str,
    requested_at: str,
) -> str:
    """Sign a harness-logs read with the hotkey, return 128-hex.

    Proves possession of the hotkey so the platform can check that hotkey owns
    ``agent_id``. Nothing is issued or stored: the signature authorizes one
    read of the miner's own diagnostics and expires with the freshness window.
    """
    payload = build_harness_logs_payload(
        hotkey_ss58=handle.hotkey_ss58,
        agent_id=agent_id,
        requested_at=requested_at,
    )
    signature_bytes = live_wallet.hotkey.sign(payload)
    return signature_bytes.hex()
