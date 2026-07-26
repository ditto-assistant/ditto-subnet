"""Mint an owner-link attestation: the exact bytes each endpoint signs.

**Signing a message does not move funds.** Every function in this module hands
a byte string to a keypair's ``sign`` method and returns a signature. Nothing
here builds, submits, or authorises an extrinsic; nothing here can transfer
TAO. That is worth stating plainly because one half of a link may be proved by
a **coldkey**, and a coldkey is the key that *can* move funds. Using it to sign
an attestation does not: the signature covers the literal string below, it is
not a transaction, and the chain never sees it.

What a link is
--------------
A miner who rotates keys loses the platform's same-owner copy-screening
exemption, because that exemption keys on the payment-time coldkey. The
principle is owner-based -- copying is only a threat *across* owners -- but the
implementation is key-based, so a miner who moves to a new coldkey/hotkey gets
copy-flagged against their own earlier work.

An owner-link attestation says: **these two hotkeys are the same operator.**
It is symmetric -- there is no "old" and "new" side -- and **both** endpoints
must sign, because a one-sided link would let anyone name a hotkey they do not
control and then resubmit that miner's work under cover of the link.

Wire format
-----------
The pair is sorted lexicographically into ``(hotkey_lo, hotkey_hi)`` so a pair
has exactly one signable representation. Each side signs one UTF-8 message::

    ditto-owner-link:v1:{netuid}:{hotkey_lo}:{hotkey_hi}:{nonce}:{issued}:{side}:{key_kind}:{signer}

``nonce`` is the ``str()`` form of a :class:`uuid.UUID`; ``issued`` is
``issued_at.astimezone(UTC).isoformat(timespec="microseconds")``; ``side`` is
``lo`` or ``hi``; ``key_kind`` is ``hotkey`` or ``coldkey``; ``signer`` is the
SS58 that actually signs (the hotkey itself for a hotkey proof, the owning
coldkey for a coldkey proof). Each signature travels as the ``.hex()`` of the
64-byte sr25519 signature (128 lowercase hex chars).

Every field is inside the signed bytes for a reason: the domain tag keeps a
signature minted here from being replayed into the upload or validator signing
lanes, the netuid keeps an attestation minted on another subnet off SN118, the
canonical pair stops the two halves disagreeing about which link they are for,
``side`` stops a half being moved onto the other endpoint, ``key_kind`` +
``signer`` stop a half being passed off as a stronger key type than the one
that produced it, the nonce is the server's replay guard, and ``issued_at``
bounds how long a leaked-but-unsubmitted signature stays useful.

The server-side verifier is ``ditto/api_server/attestation.py`` in
``ditto-platform`` (``canonical_pair`` / ``link_message`` / ``verify_link``).
The builders below are a byte-for-byte mirror of it; any drift on either side
breaks every attestation. They are kept pure -- no wallet, no clock, no
environment -- so both repositories can pin them against identical vectors.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    import bittensor

LINK_DOMAIN: Final = "ditto-owner-link:v1"
"""Domain tag for both halves of an owner-link attestation."""

KeyKind = Literal["hotkey", "coldkey"]
"""Which key proves a half. Recorded per side and graded, never gating."""

Side = Literal["lo", "hi"]
"""Which endpoint of the canonically-ordered pair a half belongs to."""

MAX_LINK_DEPTH: Final = 1
"""Attestation hops the platform honours. Direct edges only, not transitive.

Mirrored from the platform constant of the same name so the CLI can say the
same thing the server enforces: ``A--B`` plus ``B--C`` does **not** link ``A``
and ``C``, because those two owners never signed anything with each other. A
miner on their third hotkey attests each pair that actually collides.
"""


def canonical_pair(hotkey_a: str, hotkey_b: str) -> tuple[str, str]:
    """Return the two hotkeys in canonical (sorted) order as ``(lo, hi)``.

    The link is symmetric, so a pair must have exactly one signable
    representation. Sorting gives that for free and means the same two miners
    cannot create two distinct edges by submitting the pair in either order.

    Args:
        hotkey_a: One endpoint's SS58 address, in any order.
        hotkey_b: The other endpoint's SS58 address, in any order.

    Returns:
        ``(hotkey_lo, hotkey_hi)``, lexicographically sorted.
    """
    return (hotkey_a, hotkey_b) if hotkey_a <= hotkey_b else (hotkey_b, hotkey_a)


def link_message(
    *,
    netuid: int,
    hotkey_lo: str,
    hotkey_hi: str,
    nonce: UUID,
    issued_at: datetime,
    side: Side,
    key_kind: KeyKind,
    signer: str,
) -> bytes:
    """Return the exact UTF-8 bytes one endpoint signs.

    Called twice per attestation -- once per side -- with the same pair, nonce
    and timestamp.

    Args:
        netuid: Subnet the attestation is minted for. Bound into the payload
            so it cannot be replayed onto a different subnet.
        hotkey_lo: Lower of the two hotkeys, per :func:`canonical_pair`.
        hotkey_hi: Higher of the two hotkeys, per :func:`canonical_pair`.
        nonce: Single-use UUID; the platform stores it under a unique
            constraint so a captured attestation cannot be submitted twice.
        issued_at: Mint time. Serialised as an ISO-8601 UTC timestamp with
            microsecond precision, exactly as the verifier rebuilds it.
        side: ``"lo"`` or ``"hi"`` -- which endpoint this half is for.
        key_kind: ``"hotkey"`` or ``"coldkey"`` -- how this half is proved.
        signer: SS58 that produces the signature. Equals this side's hotkey
            for a ``hotkey`` proof, the owning coldkey for a ``coldkey`` one.

    Returns:
        The signing payload for this side.
    """
    issued = issued_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (
        f"{LINK_DOMAIN}:{netuid}:{hotkey_lo}:{hotkey_hi}:{nonce}:{issued}"
        f":{side}:{key_kind}:{signer}"
    ).encode()


def signer_address(*, live_wallet: bittensor.Wallet, key_kind: KeyKind) -> str:
    """Return the SS58 that will prove a half with ``key_kind``.

    Reads the coldkey's **public** side for a coldkey proof, so resolving the
    address never asks for a password; only the signature itself does.

    Args:
        live_wallet: Live bittensor wallet for the side being proved.
        key_kind: ``"hotkey"`` to prove with the hotkey itself, ``"coldkey"``
            to prove with the coldkey that owns it.

    Returns:
        The SS58 address that must appear as ``signer`` in the signed bytes.
    """
    if key_kind == "hotkey":
        hotkey_ss58: str = live_wallet.hotkey.ss58_address
        return hotkey_ss58
    coldkey_ss58: str = live_wallet.coldkeypub.ss58_address
    return coldkey_ss58


def sign_link_half(
    *,
    live_wallet: bittensor.Wallet,
    netuid: int,
    hotkey_lo: str,
    hotkey_hi: str,
    nonce: UUID,
    issued_at: datetime,
    side: Side,
    key_kind: KeyKind,
) -> str:
    """Sign one half of the link and return the hex signature.

    **No funds move.** This signs the string built by :func:`link_message` and
    nothing else. A ``coldkey`` proof decrypts the coldkey (so the bittensor
    SDK will prompt for its password) purely to produce that signature; it
    does not construct or submit any extrinsic.

    Args:
        live_wallet: Live bittensor wallet holding this side's hotkey. Passed
            separately from the frozen
            :class:`~ditto.miner_cli.models.WalletHandle` for the same reason
            :func:`ditto.miner_cli.signing.sign_upload_payload` does it.
        netuid: Subnet the attestation is minted for.
        hotkey_lo: Lower of the two hotkeys, per :func:`canonical_pair`.
        hotkey_hi: Higher of the two hotkeys, per :func:`canonical_pair`.
        nonce: Single-use UUID shared with the other half.
        issued_at: Mint time shared with the other half.
        side: Which endpoint of the pair this wallet's hotkey occupies.
        key_kind: ``"hotkey"`` to sign with ``live_wallet.hotkey``,
            ``"coldkey"`` to sign with ``live_wallet.coldkey``.

    Returns:
        The 128-char lowercase hex sr25519 signature. The server validates the
        shape with ``_SIGNATURE_HEX_PATTERN`` in
        :mod:`ditto.api_models.attestation`.
    """
    signer = signer_address(live_wallet=live_wallet, key_kind=key_kind)
    payload = link_message(
        netuid=netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=nonce,
        issued_at=issued_at,
        side=side,
        key_kind=key_kind,
        signer=signer,
    )
    keypair = live_wallet.hotkey if key_kind == "hotkey" else live_wallet.coldkey
    signature_bytes: bytes = keypair.sign(payload)
    return signature_bytes.hex()
