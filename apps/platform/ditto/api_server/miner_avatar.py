"""Signed miner profile-picture proofs and image sniffing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from ditto.api_server.attestation import (
    MAX_ATTESTATION_AGE,
    MAX_ISSUED_AT_SKEW,
    verify_signature,
)
from ditto.api_server.name_claim import KeyKind, NameClaimRejected

SET_DOMAIN: Final = "ditto-miner-avatar:v1"
CLEAR_DOMAIN: Final = "ditto-miner-avatar-clear:v1"
MAX_AVATAR_BYTES: Final = 512 * 1024

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_WEBP_RIFF = b"RIFF"
_WEBP_WEBP = b"WEBP"


class MinerAvatarRejected(NameClaimRejected):
    """A profile-picture set or clear failed verification or policy."""


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
        f"{CLEAR_DOMAIN}:{netuid}:{miner_hotkey}:{nonce}:{issued}:{key_kind}:{signer}"
    ).encode()


def check_freshness(*, issued_at: datetime, now: datetime) -> None:
    issued = issued_at.astimezone(UTC)
    if issued > now + MAX_ISSUED_AT_SKEW:
        raise MinerAvatarRejected(
            "avatar issued_at is in the future beyond the allowed skew"
        )
    if issued < now - MAX_ATTESTATION_AGE:
        raise MinerAvatarRejected(
            "avatar signature has expired; mint a fresh one and submit it again"
        )


def verify_signed_action(
    *,
    payload: bytes,
    hotkey: str,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    bound_coldkey: str | None,
) -> None:
    if key_kind == "hotkey":
        if signer != hotkey:
            raise MinerAvatarRejected(
                "hotkey proof signer must be the named hotkey itself"
            )
    elif key_kind == "coldkey":
        if bound_coldkey is None:
            raise MinerAvatarRejected(
                "coldkey proof requires a payment record binding that hotkey"
            )
        if signer != bound_coldkey:
            raise MinerAvatarRejected(
                "coldkey proof signer is not the payment-bound coldkey"
            )
    else:
        raise MinerAvatarRejected(f"unknown key_kind {key_kind!r}")
    if not verify_signature(signer=signer, payload=payload, signature_hex=signature):
        raise MinerAvatarRejected("signature did not verify")


def sniff_image(body: bytes) -> tuple[str, Literal["png", "jpg", "webp"]]:
    """Return content-type and extension, or raise if the bytes are not an image."""
    if len(body) > MAX_AVATAR_BYTES:
        raise MinerAvatarRejected(f"avatar exceeds {MAX_AVATAR_BYTES} bytes")
    if len(body) < 12:
        raise MinerAvatarRejected("avatar is too small to be an image")
    if body.startswith(_PNG):
        return "image/png", "png"
    if body.startswith(_JPEG):
        return "image/jpeg", "jpg"
    if body[:4] == _WEBP_RIFF and body[8:12] == _WEBP_WEBP:
        return "image/webp", "webp"
    raise MinerAvatarRejected("avatar must be a PNG, JPEG, or WebP image")


def public_avatar_path(hotkey: str) -> str:
    return f"/api/v1/public/miners/{hotkey}/avatar"
