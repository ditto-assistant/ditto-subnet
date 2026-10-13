"""Tests for SN118 CLA signing, append-only immutability, upload gate modes, and Backroom metrics (#2872)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apps.platform.ditto.api_server.cla import (
    AppendOnlyClaStore,
    ClaRejectedError,
    cla_message as server_cla_message,
)
from ditto.miner_cli.cla import (
    cla_message as cli_cla_message,
    compute_cla_sha256,
    verify_cla_digest,
)

SAMPLE_CLA_V1 = "SN118 CLA v1 placeholder fixture text"
SAMPLE_CLA_V2 = "SN118 CLA v2 updated placeholder fixture text"


def _fake_verify(*, signer: str, payload: bytes, signature_hex: str) -> bool:
    expected = hashlib.sha256(signer.encode() + b":" + payload).hexdigest()
    return signature_hex == expected


def _fake_sign(signer: str, payload: bytes) -> str:
    return hashlib.sha256(signer.encode() + b":" + payload).hexdigest()


def test_cli_and_server_message_builders_match_byte_for_byte() -> None:
    nonce = uuid.uuid4()
    issued_at = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
    digest = compute_cla_sha256(SAMPLE_CLA_V1)
    c_bytes = cli_cla_message(
        netuid=118,
        cla_version=1,
        cla_sha256=digest,
        hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        nonce=nonce,
        issued_at=issued_at,
    )
    s_bytes = server_cla_message(
        netuid=118,
        cla_version=1,
        cla_sha256=digest,
        hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        nonce=nonce,
        issued_at=issued_at,
    )
    assert c_bytes == s_bytes


def test_cli_refuses_on_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="CLA SHA-256 mismatch"):
        verify_cla_digest(content=SAMPLE_CLA_V1, expected_sha256="0" * 64)


def test_append_only_store_forbids_update_and_delete() -> None:
    store = AppendOnlyClaStore()
    with pytest.raises(PermissionError, match="append-only"):
        store.update_signature()
    with pytest.raises(PermissionError, match="append-only"):
        store.delete_signature()


def test_upload_gate_modes_and_security_rejections() -> None:
    store = AppendOnlyClaStore()
    v1 = store.register_version(version=1, text=SAMPLE_CLA_V1)
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    now = datetime.now(UTC)

    # 1. Mode 'off' accepts unsigned hotkey
    assert store.check_upload_gate(hotkey, now=now) is True

    # 2. Mode 'shadow' logs & counts unsigned upload, but accepts it
    store.set_gate_mode(mode="shadow", changed_by="admin")
    assert store.check_upload_gate(hotkey, now=now) is True
    summary = store.backroom_adoption_summary([hotkey], now=now)
    assert summary["shadow_unsigned_upload_attempts"] == 1
    assert summary["unsigned_active_hotkeys"] == 1

    # 3. Mode 'enforce' rejects unsigned hotkey with instructions to run 'ditto cla sign'
    store.set_gate_mode(mode="enforce", changed_by="admin")
    with pytest.raises(ClaRejectedError, match="uv run ditto cla sign"):
        store.check_upload_gate(hotkey, now=now)

    # 4. Wrong signer is rejected
    nonce1 = uuid.uuid4()
    payload1 = server_cla_message(
        netuid=118, cla_version=1, cla_sha256=v1.sha256, hotkey=hotkey, nonce=nonce1, issued_at=now
    )
    with pytest.raises(ClaRejectedError, match="wrong signer"):
        store.record_signature(
            netuid=118,
            hotkey=hotkey,
            signer="5DifferentHotkey",
            cla_version=1,
            cla_sha256=v1.sha256,
            signature_hex=_fake_sign(hotkey, payload1),
            nonce=nonce1,
            issued_at=now,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdkeyOwner",
            now=now,
        )

    # 5. Stale issued_at is rejected
    stale_time = now - timedelta(minutes=25)
    stale_payload = server_cla_message(
        netuid=118, cla_version=1, cla_sha256=v1.sha256, hotkey=hotkey, nonce=uuid.uuid4(), issued_at=stale_time
    )
    with pytest.raises(ClaRejectedError, match="stale issued_at"):
        store.record_signature(
            netuid=118,
            hotkey=hotkey,
            signer=hotkey,
            cla_version=1,
            cla_sha256=v1.sha256,
            signature_hex=_fake_sign(hotkey, stale_payload),
            nonce=uuid.uuid4(),
            issued_at=stale_time,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdkeyOwner",
            now=now,
        )

    # 6. Valid signature succeeds and passes enforce gate
    store.record_signature(
        netuid=118,
        hotkey=hotkey,
        signer=hotkey,
        cla_version=1,
        cla_sha256=v1.sha256,
        signature_hex=_fake_sign(hotkey, payload1),
        nonce=nonce1,
        issued_at=now,
        verify_fn=_fake_verify,
        resolve_owner_coldkey_fn=lambda _: "5ColdkeyOwner",
        now=now,
    )
    assert store.check_upload_gate(hotkey, now=now) is True

    # 7. Replayed nonce is rejected
    with pytest.raises(ClaRejectedError, match="replayed nonce"):
        store.record_signature(
            netuid=118,
            hotkey=hotkey,
            signer=hotkey,
            cla_version=1,
            cla_sha256=v1.sha256,
            signature_hex=_fake_sign(hotkey, payload1),
            nonce=nonce1,
            issued_at=now,
            verify_fn=_fake_verify,
            resolve_owner_coldkey_fn=lambda _: "5ColdkeyOwner",
            now=now,
        )

    # 8. Publishing v2 makes v1 outdated for new uploads until re-signed
    store.register_version(version=2, text=SAMPLE_CLA_V2, effective_at=now)
    with pytest.raises(ClaRejectedError, match="uv run ditto cla sign"):
        store.check_upload_gate(hotkey, now=now)
