"""Mint a hotkey-rotation attestation: the exact bytes each wallet signs.

A miner who rotates hotkeys loses the platform's same-owner copy-screening
exemption, because that exemption keys on the payment-time coldkey. The
principle is owner-based -- copying is only a threat *across* owners -- but the
implementation is key-based, so a miner who moves to a new coldkey/hotkey pair
gets copy-flagged against their own earlier work. A signed attestation is the
self-serve fix: the **old** hotkey states that a **new** hotkey continues it,
and the **new** hotkey counter-signs to accept.

Wire format
-----------
Two UTF-8 messages over the same tuple, differing only in the domain tag::

    ditto-hotkey-attestation:v1:{netuid}:{old_hotkey}:{new_hotkey}:{nonce}:{issued}
    ditto-hotkey-attestation-accept:v1:{netuid}:{old_hotkey}:{new_hotkey}:{nonce}:{issued}

``nonce`` is the ``str()`` form of a :class:`uuid.UUID`. ``issued`` is
``issued_at.astimezone(UTC).isoformat(timespec="microseconds")``. The first is
signed by ``old_hotkey``, the second by ``new_hotkey``; each signature travels
as the ``.hex()`` of the 64-byte sr25519 signature (128 lowercase hex chars).

Why the shape matters: the domain tag keeps a signature minted here from being
replayed into the upload or validator signing lanes, the netuid keeps an
attestation minted on another subnet off SN118, both hotkeys in fixed order
make the direction signed rather than inferred, the nonce is the server's
replay guard, and ``issued_at`` bounds how long a leaked-but-unsubmitted
signature stays useful.

The server-side verifier is ``ditto/api_server/attestation.py`` in
``ditto-platform`` (``attestation_message`` / ``acceptance_message`` /
``verify_attestation_pair``). The builders below are a byte-for-byte mirror of
it; any drift on either side breaks every attestation. The builders are kept
pure -- no wallet, no clock, no environment -- so both repositories can pin
them against identical test vectors.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    import bittensor

ATTESTATION_DOMAIN: Final = "ditto-hotkey-attestation:v1"
"""Domain tag for the half signed by the OLD (predecessor) hotkey."""

ACCEPTANCE_DOMAIN: Final = "ditto-hotkey-attestation-accept:v1"
"""Domain tag for the half signed by the NEW (successor) hotkey."""


def attestation_message(
    *,
    netuid: int,
    old_hotkey: str,
    new_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> bytes:
    """Return the exact UTF-8 bytes the OLD hotkey signs.

    Args:
        netuid: Subnet the attestation is minted for. Bound into the payload
            so it cannot be replayed onto a different subnet.
        old_hotkey: SS58 address of the predecessor hotkey (the signer).
        new_hotkey: SS58 address of the successor hotkey.
        nonce: Single-use UUID; the platform stores it under a unique
            constraint so a captured attestation cannot be submitted twice.
        issued_at: Mint time. Serialised as an ISO-8601 UTC timestamp with
            microsecond precision, exactly as the verifier rebuilds it.

    Returns:
        The signing payload. Paired with :func:`acceptance_message` over the
        same tuple.
    """
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"{ATTESTATION_DOMAIN}:{netuid}:{old_hotkey}:{new_hotkey}:{nonce}:{issued}"
    ).encode()


def acceptance_message(
    *,
    netuid: int,
    old_hotkey: str,
    new_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> bytes:
    """Return the exact UTF-8 bytes the NEW hotkey signs to accept.

    Binds the identical tuple as :func:`attestation_message`, differing only in
    the domain tag. Sharing the nonce is what makes the pair inseparable: an
    acceptance minted for one attestation cannot be lifted onto another.
    """
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"{ACCEPTANCE_DOMAIN}:{netuid}:{old_hotkey}:{new_hotkey}:{nonce}:{issued}"
    ).encode()


def sign_attestation(
    *,
    live_wallet: bittensor.Wallet,
    netuid: int,
    old_hotkey: str,
    new_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> str:
    """Sign the attestation half with the OLD wallet's hotkey, return hex.

    Args:
        live_wallet: Live bittensor wallet whose hotkey is ``old_hotkey``.
            Passed separately from the frozen
            :class:`~ditto.miner_cli.models.WalletHandle` for the same reason
            :func:`ditto.miner_cli.signing.sign_upload_payload` does it.
        netuid: Subnet the attestation is minted for.
        old_hotkey: SS58 address of the signing (predecessor) hotkey.
        new_hotkey: SS58 address of the successor hotkey.
        nonce: Single-use UUID shared with the acceptance half.
        issued_at: Mint time shared with the acceptance half.

    Returns:
        The 128-char lowercase hex sr25519 signature. The server validates the
        shape with ``_SIGNATURE_HEX_PATTERN`` in
        :mod:`ditto.api_models.attestation`.
    """
    payload = attestation_message(
        netuid=netuid,
        old_hotkey=old_hotkey,
        new_hotkey=new_hotkey,
        nonce=nonce,
        issued_at=issued_at,
    )
    signature_bytes: bytes = live_wallet.hotkey.sign(payload)
    return signature_bytes.hex()


def sign_acceptance(
    *,
    live_wallet: bittensor.Wallet,
    netuid: int,
    old_hotkey: str,
    new_hotkey: str,
    nonce: UUID,
    issued_at: datetime,
) -> str:
    """Sign the acceptance half with the NEW wallet's hotkey, return hex.

    Same arguments as :func:`sign_attestation`, except ``live_wallet`` must
    hold ``new_hotkey``. The nonce and ``issued_at`` MUST be the identical
    values used for the attestation half, or the platform rejects the pair.
    """
    payload = acceptance_message(
        netuid=netuid,
        old_hotkey=old_hotkey,
        new_hotkey=new_hotkey,
        nonce=nonce,
        issued_at=issued_at,
    )
    signature_bytes: bytes = live_wallet.hotkey.sign(payload)
    return signature_bytes.hex()
