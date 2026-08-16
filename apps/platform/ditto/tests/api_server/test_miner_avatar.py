"""Pure tests for signed miner-avatar payloads and image sniffing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bittensor
import pytest

from ditto.api_server.miner_avatar import (
    MAX_AVATAR_BYTES,
    MinerAvatarRejected,
    check_freshness,
    clear_message,
    public_avatar_path,
    set_message,
    sniff_image,
    verify_signed_action,
)

_ISSUED = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_NONCE = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_sniff_accepts_png_jpeg_webp() -> None:
    assert sniff_image(_PNG) == ("image/png", "png")
    assert sniff_image(b"\xff\xd8\xff" + b"\x00" * 16) == ("image/jpeg", "jpg")
    assert sniff_image(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 4) == (
        "image/webp",
        "webp",
    )


def test_sniff_rejects_unknown_and_oversize() -> None:
    with pytest.raises(MinerAvatarRejected, match="PNG, JPEG, or WebP"):
        sniff_image(b"GIF89a" + b"\x00" * 16)
    with pytest.raises(MinerAvatarRejected, match="too small"):
        sniff_image(b"\x89PNG")
    with pytest.raises(MinerAvatarRejected, match="exceeds"):
        sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_AVATAR_BYTES))


def test_set_and_clear_messages_are_stable() -> None:
    assert set_message(
        netuid=118,
        miner_hotkey=_HOTKEY,
        content_sha256="a" * 64,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=_HOTKEY,
    ) == (
        b"ditto-miner-avatar:v1:118:"
        + _HOTKEY.encode()
        + b":"
        + b"a" * 64
        + b":aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:"
        + b"2026-08-16T12:00:00.000000+00:00:hotkey:"
        + _HOTKEY.encode()
    )
    assert clear_message(
        netuid=118,
        miner_hotkey=_HOTKEY,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=_HOTKEY,
    ).startswith(b"ditto-miner-avatar-clear:v1:118:")


def test_freshness_rejects_future_and_stale() -> None:
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
    check_freshness(issued_at=now, now=now)
    with pytest.raises(MinerAvatarRejected, match="future"):
        check_freshness(issued_at=now + timedelta(hours=1), now=now)
    with pytest.raises(MinerAvatarRejected, match="expired"):
        check_freshness(issued_at=now - timedelta(days=2), now=now)


def test_hotkey_signature_verifies() -> None:
    kp = bittensor.Keypair.create_from_uri("//Alice")
    payload = set_message(
        netuid=118,
        miner_hotkey=kp.ss58_address,
        content_sha256="b" * 64,
        nonce=_NONCE,
        issued_at=_ISSUED,
        key_kind="hotkey",
        signer=kp.ss58_address,
    )
    verify_signed_action(
        payload=payload,
        hotkey=kp.ss58_address,
        key_kind="hotkey",
        signer=kp.ss58_address,
        signature=kp.sign(payload).hex(),
        bound_coldkey=None,
    )


def test_hotkey_proof_must_name_the_hotkey() -> None:
    with pytest.raises(MinerAvatarRejected, match="named hotkey"):
        verify_signed_action(
            payload=b"x",
            hotkey=_HOTKEY,
            key_kind="hotkey",
            signer="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            signature="aa",
            bound_coldkey=None,
        )


def test_public_avatar_path() -> None:
    assert public_avatar_path(_HOTKEY) == f"/api/v1/public/miners/{_HOTKEY}/avatar"
